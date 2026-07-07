"""Summarize a scanned PDF in Italian via the Claude API.

The on-demand half of the Telegram integration: nothing here runs during
scanning or uploading. It fires only when the user taps the "Summarize" button,
at which point bot.py fetches the PDF back from Drive and calls summarize_pdf().

The PDFs are scanned *images* (no text layer), so we hand the whole PDF to a
vision-capable model and let it read the pages — no separate OCR step. Like
rclone and the Telegram POST, the Anthropic API call is an untested,
hardware-like seam; the pure request-building bits below are tested.
"""

from __future__ import annotations

import base64
import logging

log = logging.getLogger("mailscan")

# Cheapest vision-capable model; plenty for a few scanned pages. Overridable via
# `summary_model` in config.
SUMMARY_MODEL = "claude-haiku-4-5"
SUMMARY_MAX_TOKENS = 1024

# Language the model writes its answer in. This is the *only* place the output
# language is set — everything else in the code and config stays English.
# Overridable via `summary_language` in config.
DEFAULT_SUMMARY_LANGUAGE = "Italian"


def build_prompt(language: str = DEFAULT_SUMMARY_LANGUAGE) -> str:
    """The instruction: three fixed sections — Summary / Actions / Critical —
    written entirely in `language` (headers localized), each emoji-prefixed so
    the result stays readable after HTML-escaping in the Telegram reply. Pure."""
    return (
        "This is a scanned paper document (postal mail, a bill, a letter, or an "
        f"official communication). Analyze it and reply entirely in {language}, "
        "clearly and concisely, using exactly these three sections. Localize the "
        f"section headers into {language} and prefix each with the emoji shown:\n\n"
        "📄 Summary — in 1-3 sentences: what it is, who sent it, the document "
        "type and date.\n"
        "✅ Actions — what needs to be done and any deadlines; state that none "
        "are required if so.\n"
        "⚠️ Critical — amounts, penalties, critical dates, or consequences not "
        "to overlook; state that none apply if so.\n\n"
        "Write in plain text only. Do NOT use Markdown or any formatting symbols "
        "(no **, *, _, #, backticks, or bullet markers). "
        "If the document is unreadable or blank, say so explicitly. Reply with "
        "only the three sections, no preamble."
    )


def build_pdf_message(pdf_bytes: bytes, prompt: str) -> list[dict]:
    """The Messages `messages` payload: the PDF as a base64 document block,
    followed by the instruction. Pure — no network, no SDK import."""
    data = base64.standard_b64encode(pdf_bytes).decode("ascii")
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": data,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }
    ]


def summarize_pdf(
    api_key: str,
    pdf_bytes: bytes,
    *,
    model: str = SUMMARY_MODEL,
    language: str = DEFAULT_SUMMARY_LANGUAGE,
    max_tokens: int = SUMMARY_MAX_TOKENS,
) -> str:
    """Call Claude to summarize the scanned PDF; return the answer in `language`.

    Raises on API/network error — the caller (bot.py) turns that into a logged,
    best-effort failure reply. `anthropic` is imported lazily so the scanner and
    upload paths never need the dependency loaded."""
    import anthropic  # lazy: keep it off the scanning/upload hot paths

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=build_pdf_message(pdf_bytes, build_prompt(language)),
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()
