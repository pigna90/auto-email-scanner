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


def test_format_message_includes_name_and_link():
    msg = upload.format_message("mail_2026-07-07_101500.pdf", "https://x/y")
    assert "mail_2026-07-07_101500.pdf" in msg
    assert "https://x/y" in msg


def test_format_message_without_link_notes_it():
    msg = upload.format_message("mail.pdf", None)
    assert "mail.pdf" in msg
    assert "unavailable" in msg.lower()


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
    monkeypatch.setattr(
        upload, "_notify", lambda c, name, link: notified.append((name, link))
    )

    count = upload.upload_pending(cfg)

    assert count == 1
    assert moved == ["mail_old.pdf"]
    assert notified == [
        ("mail_old.pdf", "https://drive.google.com/file/d/ID_mail_old.pdf/view")
    ]


def test_upload_pending_no_dir_is_noop(tmp_path):
    cfg = Config(output_dir=tmp_path / "does-not-exist")
    assert upload.upload_pending(cfg) == 0
