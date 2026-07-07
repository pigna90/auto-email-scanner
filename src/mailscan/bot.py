"""Poll Telegram for "Summarize" button taps and reply with an AI summary.

The on-demand, fully decoupled half of the pipeline (see CLAUDE.md): scanning
and uploading never call an LLM. Only when the user taps the button attached to
an upload notification does this poller — run every ~30s by mailscan-bot.timer —
wake up, pull that PDF back from Drive (by its file id), summarize it via Claude
(summarize.py, in the configured `summary_language`), and post it as a reply.

Best-effort throughout, like the uploader: any failure logs and moves on, never
crashes. State is a single file holding the Telegram getUpdates offset so we
don't reprocess taps across runs. The urllib/rclone/Anthropic calls are the
untested seam; the pure helpers (callback parsing, offset I/O, reply formatting)
are tested.
"""

from __future__ import annotations

import html
import json
import logging
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Config
from .summarize import summarize_pdf
from .upload import SUMMARY_CALLBACK_PREFIX, send_telegram

log = logging.getLogger("mailscan")

_API_TIMEOUT = 20  # seconds for a getUpdates / answerCallbackQuery round-trip


# --- pure helpers (tested) -------------------------------------------------

def parse_summary_callback(data: str | None) -> str | None:
    """Extract the PDF name from a "sum:<name>" callback, else None.

    The name is basenamed so a crafted callback can never escape the cache dir
    or the Drive remote path (the chat is owner-only, but cheap to be safe)."""
    if data and data.startswith(SUMMARY_CALLBACK_PREFIX):
        name = Path(data[len(SUMMARY_CALLBACK_PREFIX):]).name
        return name or None
    return None


def offset_path(cfg: Config) -> Path:
    """Where the getUpdates offset lives (next to the work dir)."""
    return cfg.work_dir.parent / "bot-offset"


def read_offset(path: Path) -> int:
    """Last acknowledged getUpdates offset, or 0 if unset/corrupt."""
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(offset))


def format_summary(summary: str) -> str:
    """The Telegram reply body (HTML). The model writes the three sections in
    plain text (the prompt forbids Markdown); we strip any stray bold markers it
    emits anyway, HTML-escape the rest, and add our own bold header (real HTML,
    so it renders). The header is app chrome, so it stays English."""
    clean = summary.replace("**", "").replace("__", "")
    return "🧠 <b>AI Summary</b>\n\n" + html.escape(clean)


# --- Telegram + Drive seam (untested) --------------------------------------

def _get_updates(token: str, offset: int) -> list[dict]:
    """Fetch pending callback_query updates (short poll, returns immediately)."""
    params = {"timeout": "0", "allowed_updates": '["callback_query"]'}
    if offset:
        params["offset"] = str(offset)
    url = (
        f"https://api.telegram.org/bot{token}/getUpdates?"
        + urllib.parse.urlencode(params)
    )
    with urllib.request.urlopen(url, timeout=_API_TIMEOUT) as resp:
        payload = json.loads(resp.read())
    return payload.get("result", []) if payload.get("ok") else []


def _answer_callback(token: str, callback_query_id: str, text: str | None = None) -> None:
    """Ack a tap so Telegram clears the button's spinner (best-effort)."""
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    fields = {"callback_query_id": callback_query_id}
    if text:
        fields["text"] = text
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
        resp.read()


def _fetch_pdf(cfg: Config, name: str) -> bytes:
    """Stream a PDF back from Drive as raw bytes (rclone cat)."""
    proc = subprocess.run(
        ["rclone", "cat", f"{cfg.drive_remote}/{name}"],
        capture_output=True,  # binary: no text=True
        check=True,
    )
    return proc.stdout


def _load_pdf(cfg: Config, name: str) -> bytes:
    """The PDF bytes for a summary request: the local cache copy if it's still
    there (the fast path, right after upload), else fetched back from Drive."""
    local = cfg.cache_dir / name
    if local.is_file():
        log.info("Summarizing %s from local cache", name)
        return local.read_bytes()
    log.info("Summarizing %s (not cached; fetching from Drive)", name)
    return _fetch_pdf(cfg, name)


def _reply(cfg: Config, chat_id: str, reply_to: int, text: str) -> None:
    try:
        send_telegram(cfg.telegram_bot_token, chat_id, text, reply_to=reply_to)
    except Exception:  # noqa: BLE001 - a failed reply must not crash the poller
        log.exception("Failed to send summary reply to chat %s", chat_id)


def _handle_callback(cfg: Config, cq: dict) -> None:
    """Process one "summarize" tap end-to-end, best-effort."""
    name = parse_summary_callback(cq.get("data"))
    if name is None:
        return
    token = cfg.telegram_bot_token
    message = cq.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    reply_to = message.get("message_id")
    if not chat_id or reply_to is None:
        log.warning("Callback without a chat/message to reply to; ignoring.")
        return

    try:
        _answer_callback(token, cq.get("id", ""), "Generating summary…")
    except Exception:  # noqa: BLE001 - the ack is cosmetic
        log.exception("answerCallbackQuery failed")

    if not cfg.anthropic_api_key:
        _reply(cfg, chat_id, reply_to, "⚠️ AI summary is not configured.")
        return

    try:
        pdf = _load_pdf(cfg, name)
        summary = summarize_pdf(
            cfg.anthropic_api_key,
            pdf,
            model=cfg.summary_model,
            language=cfg.summary_language,
        )
    except Exception:  # noqa: BLE001 - one bad summary must not stop the poller
        log.exception("Summary failed for %s", name)
        _reply(cfg, chat_id, reply_to, "⚠️ Failed to generate the summary.")
        return

    _reply(cfg, chat_id, reply_to, format_summary(summary))
    log.info("Posted summary for %s", name)


def poll_once(cfg: Config) -> int:
    """One getUpdates poll: handle every pending tap, advance the offset.

    Returns the number of summary requests handled. Called by `mailscan bot`,
    which mailscan-bot.timer fires on a schedule.
    """
    token = cfg.telegram_bot_token
    if not token:
        log.info("Telegram not configured; bot poll skipped.")
        return 0

    state = offset_path(cfg)
    offset = read_offset(state)
    try:
        updates = _get_updates(token, offset)
    except Exception:  # noqa: BLE001 - a failed poll retries next tick
        log.exception("getUpdates failed")
        return 0

    handled = 0
    last_update_id: int | None = None
    for upd in updates:
        uid = upd.get("update_id")
        if uid is not None:
            last_update_id = uid
        cq = upd.get("callback_query")
        if cq:
            try:
                _handle_callback(cfg, cq)
            except Exception:  # noqa: BLE001 - keep draining; don't wedge offset
                log.exception("Unhandled error processing a callback")
            handled += 1

    # Advance past everything we fetched even if some weren't ours, so we never
    # re-poll the same updates. Do this only after handling, so a crash mid-batch
    # leaves the batch to be retried rather than silently dropped.
    if last_update_id is not None:
        write_offset(state, last_update_id + 1)

    return handled
