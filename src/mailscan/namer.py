"""Classify each scanned PDF with one Claude call: title, category, summary.

Given a finished mail PDF, a single Claude vision call reads the whole document
and returns three things:

  * title    — a short file name (in `naming_language`, English by default),
  * category — exactly one of `config.categories` (each key is a Drive folder),
  * summary  — a short three-section summary (in `summary_language`).

upload.py uses these to move the file to `Drive/<category>/<scan-date>_<title>.pdf`
and to show the summary inline in the Telegram notification (inside an expandable
blockquote), so the whole story arrives in one message with no second LLM call.

This is a decoupled, best-effort seam — it runs in the timer-driven upload
path, never in the scanner daemon, and any failure falls
back to the original timestamp name and the default category. The category is
constrained by the response schema, but we still validate it and re-ask up to
`config.classify_max_retries` times if it's ever unknown. The pure helpers
(prompt/schema building, name sanitising, category validation, scan-date and
final-name assembly) are tested; the anthropic call is the untested seam.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from .config import Config

log = logging.getLogger("mailscan")

# Room for the title + category + a three-section summary in one response.
_CLAUDE_MAX_TOKENS = 1024


# --- pure helpers (tested) -------------------------------------------------

def classify_schema(categories: dict) -> dict:
    """JSON schema for the combined call: {title, category, summary}, with the
    category constrained to the category keys."""
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "category": {"type": "string", "enum": list(categories)},
            "summary": {"type": "string"},
        },
        "required": ["title", "category", "summary"],
        "additionalProperties": False,
    }


def build_classify_prompt(
    categories: dict, name_language: str, summary_language: str
) -> str:
    """The single-call instruction: read the scan and return a title (in
    `name_language`), one category key, and a three-section summary (in
    `summary_language`). Pure — the category descriptions guide the choice."""
    cat_lines = "\n".join(f'  - "{k}": {v}' for k, v in categories.items())
    keys = ", ".join(f'"{k}"' for k in categories)
    return (
        "This is a scanned paper document (postal mail — a bill, a letter, an "
        "insurance or medical document, or a similar official communication), "
        "usually written in German or Italian. Read it and return three fields "
        "as JSON.\n\n"
        "1. title — a specific file name of 3-5 words naming the SENDER or "
        "organisation AND the DOCUMENT TYPE. Do NOT include any date. Write it "
        f"entirely in {name_language}: translate every German or Italian word "
        f"into {name_language}, keeping only real proper names (company names "
        "such as DHL, AOK, ARD) untranslated. A single generic word is not "
        "acceptable, and there must be no file extension.\n\n"
        "2. category — choose EXACTLY ONE of the following keys and reply with "
        f"that key only (one of: {keys}):\n{cat_lines}\n\n"
        f"3. summary — written entirely in {summary_language}, in exactly three "
        "short sections, each on its own line, prefixed with the emoji shown "
        "(localise the headers):\n"
        "   \U0001F4C4 Summary — 1-3 sentences: what it is, who sent it, the "
        "type and date.\n"
        "   ✅ Actions — what must be done and any deadlines; say so if "
        "none.\n"
        "   ⚠️ Critical — amounts, penalties, key dates or "
        "consequences not to miss; say so if none.\n"
        "   Plain text only — no Markdown.\n\n"
        "Reply with JSON only, matching the requested schema."
    )


def validate_category(raw: str, categories: dict) -> str | None:
    """Return the canonical category key matching `raw` (case-insensitively), or
    None if it isn't one of the keys."""
    needle = (raw or "").strip().lower()
    for key in categories:
        if key.lower() == needle:
            return key
    return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def sanitize_name(raw: str, max_len: int = 60) -> str:
    """Turn a free-text name into a safe, lowercase, hyphen-separated slug.

    Strips accents/diacritics to ASCII, collapses everything else to single
    hyphens, and caps the length at a word boundary. Returns "" if nothing
    usable survives."""
    ascii_only = (
        unicodedata.normalize("NFKD", raw or "")
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = _SLUG_RE.sub("-", ascii_only.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len]
        # Cut back to the last word boundary so we don't end on a partial word.
        if "-" in slug:
            slug = slug[: slug.rindex("-")]
        slug = slug.rstrip("-")
    return slug


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def scan_date_from(pdf: Path) -> str:
    """The scan date as YYYY-MM-DD: taken from the timestamp session.py put in
    the file name, falling back to the file's mtime."""
    m = _DATE_RE.search(pdf.name)
    if m:
        return m.group(1)
    return datetime.fromtimestamp(pdf.stat().st_mtime).strftime("%Y-%m-%d")


def build_final_name(slug: str, scan_date: str) -> str:
    """Assemble the file stem (no extension) from the scan date and the slug:
    `<scan_date>_<slug>`, or just `<scan_date>` if the slug is empty."""
    return f"{scan_date}_{slug}" if slug else scan_date


# --- the LLM seam (untested) -----------------------------------------------

def name_via_claude(
    api_key: str, model: str, pdf_bytes: bytes, prompt: str, schema: dict
) -> dict:
    """Hand the whole PDF to a Claude vision model and return the parsed JSON
    matching `schema`. `anthropic` is imported lazily so the scanner path never
    loads it."""
    import anthropic  # lazy: keep it off the hot paths

    client = anthropic.Anthropic(api_key=api_key)
    data = base64.standard_b64encode(pdf_bytes).decode("ascii")
    resp = client.messages.create(
        model=model,
        max_tokens=_CLAUDE_MAX_TOKENS,
        messages=[
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
        ],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text or "{}")


class Classification(NamedTuple):
    """One document's LLM result: file stem (with scan date), category key, and
    the summary text to store."""
    stem: str
    category: str
    summary: str


def classify_document(cfg: Config, pdf: Path) -> Classification | None:
    """One Claude call → title, category and summary for `pdf`.

    Returns a `Classification` (file stem carrying the scan date, a valid
    category key, and the summary to store) or None if it can't be produced.

    The category is constrained to `cfg.categories` by the response schema, but
    we still validate it and, if it isn't a known key (or the call fails), re-ask
    up to `cfg.classify_max_retries` more times before giving up. Best-effort:
    the caller keeps the timestamp name / default category on None."""
    if not cfg.anthropic_api_key:
        log.info("No Anthropic API key; skipping classification of %s.", pdf.name)
        return None
    if not cfg.categories:
        log.warning("No categories configured; skipping classification.")
        return None

    prompt = build_classify_prompt(
        cfg.categories, cfg.naming_language, cfg.summary_language
    )
    schema = classify_schema(cfg.categories)
    pdf_bytes = pdf.read_bytes()

    attempts = max(1, cfg.classify_max_retries + 1)  # one call + N retries
    for attempt in range(1, attempts + 1):
        try:
            result = name_via_claude(
                cfg.anthropic_api_key, cfg.naming_claude_model, pdf_bytes, prompt, schema
            )
        except Exception:  # noqa: BLE001 - a bad call must never break the upload
            log.exception(
                "Classification call failed for %s (attempt %d/%d)",
                pdf.name, attempt, attempts,
            )
            continue

        category = validate_category(str(result.get("category", "")), cfg.categories)
        # Cap the title so the final name ("<date>_<title>.pdf") stays a sane
        # length for a file name.
        title = sanitize_name(str(result.get("title", "")), max_len=44)
        summary = str(result.get("summary", "")).strip()
        if category and title:
            return Classification(
                build_final_name(title, scan_date_from(pdf)), category, summary
            )
        log.warning(
            "Classification of %s gave an invalid/empty result "
            "(category=%r, title=%r); attempt %d/%d",
            pdf.name, result.get("category"), result.get("title"), attempt, attempts,
        )

    log.error(
        "Could not classify %s after %d attempt(s); leaving it for the fallback.",
        pdf.name, attempts,
    )
    return None
