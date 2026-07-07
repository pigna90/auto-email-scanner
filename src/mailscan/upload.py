"""Move finished PDFs to Google Drive and announce them on Telegram.

Deliberately kept out of the scanner daemon (see CLAUDE.md): a Drive or network
hang must never stall scanning. This runs as its own oneshot, triggered by the
mailscan-upload.timer.

The heavy lifting is still rclone — we shell out to it exactly as the old
`rclone move` did, just one file at a time so that after each upload we can read
back the Drive file id, build an owner-only link, and post it to Telegram.
"""

from __future__ import annotations

import html
import json
import logging
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Config

log = logging.getLogger("mailscan")

_TELEGRAM_TIMEOUT = 15  # seconds; a slow bot must not wedge the uploader


def drive_view_link(file_id: str) -> str:
    """Owner-only "open in Drive" URL for a Drive file id.

    Not a public share — it opens for anyone who can already see the file
    (i.e. you, logged into the Drive that owns it)."""
    return f"https://drive.google.com/file/d/{file_id}/view"


def format_message(name: str, link: str | None) -> str:
    """The Telegram message body (HTML) for a freshly-uploaded PDF.

    Sent with parse_mode=HTML so the URL hides behind a "Drive link" label
    instead of showing the raw address. *name* is unused in the body (kept short
    on purpose) but still logged by the caller."""
    if link:
        return (
            "\U0001F4EC New document scanned.\n"
            f'\U0001F517 <a href="{html.escape(link, quote=True)}">Drive link</a>'
        )
    return "\U0001F4EC New document scanned.\n(Drive link unavailable)"


def _rclone(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    """Run rclone, inheriting the environment (incl. RCLONE_CONFIG)."""
    return subprocess.run(
        ["rclone", *args],
        capture_output=capture,
        text=True,
        check=True,
    )


def _move_to_drive(cfg: Config, pdf: Path) -> None:
    """rclone-move one PDF to the Drive remote (verifies hash, then deletes)."""
    dest = f"{cfg.drive_remote}/{pdf.name}"
    _rclone(["moveto", str(pdf), dest, "--log-level", "INFO"])


def _drive_file_id(cfg: Config, name: str) -> str | None:
    """Read back the Drive file id for a just-uploaded PDF, or None."""
    proc = _rclone(
        ["lsjson", cfg.drive_remote, "--files-only", "--include", "/" + name],
        capture=True,
    )
    for item in json.loads(proc.stdout or "[]"):
        if item.get("Name") == name:
            return item.get("ID") or None
    return None


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """POST a message to the Telegram Bot API. Raises on HTTP/network error."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=_TELEGRAM_TIMEOUT) as resp:
        resp.read()  # drain; a 2xx with ok:true is all we need


def _notify(cfg: Config, name: str, link: str | None) -> None:
    """Best-effort Telegram notification — the PDF is already safe on Drive."""
    token, chat = cfg.telegram_bot_token, cfg.telegram_chat_id
    if not (token and chat):
        log.info("Telegram not configured; skipping notification for %s", name)
        return
    try:
        send_telegram(token, chat, format_message(name, link))
        log.info("Notified Telegram: %s", name)
    except Exception:  # noqa: BLE001 - a failed ping must not fail the upload
        log.exception("Telegram notification failed for %s", name)


def upload_pending(cfg: Config) -> int:
    """Upload every finished PDF in the output dir to Drive; notify per file.

    Returns the number of PDFs successfully uploaded. A per-file failure is
    logged and skipped (the file is left in place for the next run), so one bad
    upload never blocks the rest.
    """
    src_dir = cfg.output_dir
    if not src_dir.is_dir():
        log.info("No output dir yet (%s); nothing to upload.", src_dir)
        return 0

    now = time.time()
    pdfs = sorted(
        p for p in src_dir.glob("*.pdf")
        if now - p.stat().st_mtime >= cfg.upload_min_age
    )
    if not pdfs:
        return 0

    uploaded = 0
    for pdf in pdfs:
        name = pdf.name
        try:
            _move_to_drive(cfg, pdf)
        except subprocess.CalledProcessError as exc:
            log.error("Upload failed for %s (kept locally, will retry): %s",
                      name, (exc.stderr or "").strip() or exc)
            continue
        uploaded += 1
        log.info("Uploaded to Drive: %s", name)

        link = None
        try:
            fid = _drive_file_id(cfg, name)
            link = drive_view_link(fid) if fid else None
        except Exception:  # noqa: BLE001 - link lookup is non-critical
            log.exception("Could not resolve Drive link for %s", name)
        _notify(cfg, name, link)

    return uploaded
