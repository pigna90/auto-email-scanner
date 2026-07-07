"""Tests for the hardware-free bits of the uploader: link building, message
formatting, config/env resolution, and the pending-file selection logic.

Nothing here touches rclone, Drive or the network — the subprocess/urllib calls
are the untested seam (like scanner.py), exercised only against the real Pi."""

import os
import time
from pathlib import Path

import pytest

from mailscan import upload
from mailscan.config import Config


def test_drive_view_link_is_owner_link():
    link = upload.drive_view_link("1AbCdEf")
    assert link == "https://drive.google.com/file/d/1AbCdEf/view"


def test_format_message_links_behind_a_label():
    msg = upload.format_message("mail_2026-07-07_101500.pdf", "https://x/y")
    assert "New document scanned." in msg
    # URL is inside an <a href> — behind the "Drive link" label, not shown raw.
    assert '<a href="https://x/y">Drive link</a>' in msg


def test_format_message_without_link_notes_it():
    msg = upload.format_message("mail.pdf", None)
    assert "New document scanned." in msg
    assert "unavailable" in msg.lower()


def test_build_summary_keyboard_carries_name():
    import json

    markup = json.loads(upload.build_summary_keyboard("mail_2026-07-07_182755.pdf"))
    button = markup["inline_keyboard"][0][0]
    assert button["callback_data"] == "sum:mail_2026-07-07_182755.pdf"
    # callback_data must stay under Telegram's 64-byte limit.
    assert len(button["callback_data"].encode()) <= 64


def test_prune_cache_keeps_newest(tmp_path):
    import os

    now = time.time()
    for i in range(5):
        p = tmp_path / f"mail_{i}.pdf"
        p.write_bytes(b"%PDF")
        os.utime(p, (now + i, now + i))  # mail_4 newest, mail_0 oldest

    upload._prune_cache(tmp_path, keep=3)

    remaining = sorted(p.name for p in tmp_path.glob("*.pdf"))
    assert remaining == ["mail_2.pdf", "mail_3.pdf", "mail_4.pdf"]


def test_env_overrides_config_secrets(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'telegram_bot_token = "from-toml"\n'
        'telegram_chat_id = "111"\n'
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    cfg = Config.load(cfg_file)
    assert cfg.telegram_bot_token == "from-env"
    assert cfg.telegram_chat_id == "999"


def test_config_secrets_fall_back_to_toml(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('telegram_bot_token = "from-toml"\n')
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    cfg = Config.load(cfg_file)
    assert cfg.telegram_bot_token == "from-toml"


def test_upload_pending_skips_young_files_and_notifies(tmp_path, monkeypatch):
    out = tmp_path / "scans"
    out.mkdir()
    old = out / "mail_old.pdf"
    old.write_bytes(b"%PDF-1.4 old")
    young = out / "mail_young.pdf"
    young.write_bytes(b"%PDF-1.4 young")

    # Make only `old` older than the min age.
    now = time.time()
    os.utime(old, (now - 300, now - 300))
    os.utime(young, (now, now))

    cfg = Config(output_dir=out, upload_min_age=60.0)

    moved, notified = [], []
    monkeypatch.setattr(upload, "_move_to_drive", lambda c, p: moved.append(p.name))
    monkeypatch.setattr(upload, "_drive_file_id", lambda c, name: "ID_" + name)
    monkeypatch.setattr(upload, "_cache_pdf", lambda c, p: None)  # skip disk cache
    monkeypatch.setattr(
        upload, "_notify", lambda c, name, link: notified.append((name, link))
    )

    count = upload.upload_pending(cfg)

    assert count == 1
    assert moved == ["mail_old.pdf"]
    assert notified == [
        ("mail_old.pdf", "https://drive.google.com/file/d/ID_mail_old.pdf/view")
    ]


def test_notify_attaches_button_only_with_key(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        upload,
        "send_telegram",
        lambda t, c, text, **kw: sent.update(reply_markup=kw.get("reply_markup")),
    )

    # Key configured → button present, callback carries the PDF name.
    cfg = Config(
        telegram_bot_token="t", telegram_chat_id="c", anthropic_api_key="k"
    )
    upload._notify(cfg, "mail.pdf", "https://x/y")
    assert sent["reply_markup"] and "sum:mail.pdf" in sent["reply_markup"]

    # No key → no button.
    sent.clear()
    cfg_nokey = Config(telegram_bot_token="t", telegram_chat_id="c")
    upload._notify(cfg_nokey, "mail.pdf", "https://x/y")
    assert sent["reply_markup"] is None


def test_cache_pdf_copies_and_prunes(tmp_path, monkeypatch):
    out = tmp_path / "scans"
    out.mkdir()
    pdf = out / "mail_new.pdf"
    pdf.write_bytes(b"%PDF-1.4 body")
    cache = tmp_path / "cache"
    cfg = Config(cache_dir=cache, local_cache_size=2)

    upload._cache_pdf(cfg, pdf)

    assert (cache / "mail_new.pdf").read_bytes() == b"%PDF-1.4 body"


def test_upload_pending_no_dir_is_noop(tmp_path):
    cfg = Config(output_dir=tmp_path / "does-not-exist")
    assert upload.upload_pending(cfg) == 0
