"""Tests for the hardware-free bits of the summary bot: callback parsing, offset
persistence, reply formatting, and the poll loop's update-draining/offset logic.

Nothing here touches Telegram, rclone, or the Anthropic API — those are the
untested seam, monkeypatched out below."""

from pathlib import Path

import pytest

from mailscan import bot
from mailscan.config import Config


def test_parse_summary_callback():
    assert bot.parse_summary_callback("sum:mail_x.pdf") == "mail_x.pdf"
    assert bot.parse_summary_callback("sum:") is None      # no name
    assert bot.parse_summary_callback("other:mail_x.pdf") is None
    assert bot.parse_summary_callback(None) is None
    # basenamed — a crafted callback can't escape the cache/remote dir.
    assert bot.parse_summary_callback("sum:../../etc/x.pdf") == "x.pdf"


def test_load_pdf_prefers_local_cache(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "mail_x.pdf").write_bytes(b"%PDF local")
    cfg = Config(cache_dir=cache)

    # Drive fetch must NOT be called when the file is cached locally.
    monkeypatch.setattr(
        bot, "_fetch_pdf", lambda c, n: pytest.fail("should not hit Drive")
    )
    assert bot._load_pdf(cfg, "mail_x.pdf") == b"%PDF local"


def test_load_pdf_falls_back_to_drive(tmp_path, monkeypatch):
    cfg = Config(cache_dir=tmp_path / "empty")  # nothing cached
    monkeypatch.setattr(bot, "_fetch_pdf", lambda c, n: b"%PDF from drive")
    assert bot._load_pdf(cfg, "mail_x.pdf") == b"%PDF from drive"


def test_offset_roundtrip(tmp_path):
    p = tmp_path / "nested" / "bot-offset"
    assert bot.read_offset(p) == 0          # missing file → 0
    bot.write_offset(p, 42)
    assert bot.read_offset(p) == 42


def test_read_offset_tolerates_corrupt_file(tmp_path):
    p = tmp_path / "bot-offset"
    p.write_text("not-a-number")
    assert bot.read_offset(p) == 0


def test_format_summary_escapes_and_headers():
    body = bot.format_summary("amount < 10 & due")
    assert "AI Summary" in body
    assert "&lt; 10 &amp; due" in body  # HTML-escaped for parse_mode=HTML


def test_format_summary_strips_stray_markdown_bold():
    body = bot.format_summary("Total is **38,00 EUR** and __due__ soon")
    # No leftover Markdown markers reach the user.
    assert "**" not in body and "__" not in body
    assert "38,00 EUR" in body and "due" in body


def test_offset_path_sits_beside_work_dir():
    cfg = Config(work_dir=Path("/x/y/work"))
    assert bot.offset_path(cfg) == Path("/x/y/bot-offset")


def test_poll_once_handles_taps_and_advances_offset(tmp_path, monkeypatch):
    cfg = Config(
        telegram_bot_token="t",
        telegram_chat_id="c",
        anthropic_api_key="k",
        work_dir=tmp_path / "work",
    )

    updates = [
        {"update_id": 5, "callback_query": {"id": "q1", "data": "sum:doc.pdf",
         "message": {"message_id": 100, "chat": {"id": 55}}}},
        {"update_id": 6, "message": {"text": "not a callback"}},  # ignored
    ]
    monkeypatch.setattr(bot, "_get_updates", lambda token, offset: updates)

    handled_ids = []
    monkeypatch.setattr(
        bot, "_handle_callback", lambda c, cq: handled_ids.append(cq["data"])
    )

    n = bot.poll_once(cfg)

    assert n == 1
    assert handled_ids == ["sum:doc.pdf"]
    # Offset advanced past the highest update_id seen (6), so 6+1 = 7.
    assert bot.read_offset(bot.offset_path(cfg)) == 7


def test_poll_once_no_token_is_noop(tmp_path):
    cfg = Config(work_dir=tmp_path / "work")  # no telegram token
    assert bot.poll_once(cfg) == 0


def test_handle_callback_without_key_reports_it(monkeypatch):
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c")  # no key
    monkeypatch.setattr(bot, "_answer_callback", lambda *a, **k: None)

    replies = []
    monkeypatch.setattr(
        bot, "_reply", lambda c, chat, to, text: replies.append(text)
    )

    bot._handle_callback(
        cfg,
        {"id": "q", "data": "sum:doc.pdf",
         "message": {"message_id": 1, "chat": {"id": 9}}},
    )
    assert replies and "not configured" in replies[0]
