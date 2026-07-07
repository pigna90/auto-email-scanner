"""The scan daemon: wait for wake, scan sheets, group them into PDFs.

Designed to never die: a bad page or a scanner hiccup is logged and skipped,
and any unexpected error drops back to the idle/wait state instead of crashing.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from . import scanner
from .config import Config
from .merge import images_to_pdf, save_page_image, unique_path, validate_image
from .scanner import ScanResult
from .triggers import Action, Context, Delimiter, TimeoutDelimiter

log = logging.getLogger("mailscan")


def build_delimiter(cfg: Config) -> Delimiter:
    return TimeoutDelimiter(cfg.email_timeout, cfg.session_idle_timeout)


def run_daemon(cfg: Config, delimiter: Delimiter | None = None) -> None:
    """Top-level loop: idle → wait for wake → run a session → repeat forever."""
    delimiter = delimiter or build_delimiter(cfg)
    log.info("mailscan started. Output dir: %s", cfg.output_dir)
    while True:
        try:
            if not scanner.present(cfg):
                if cfg.require_wake:
                    log.info("Idle — waiting for the scanner to wake "
                             "(press its button / feed a sheet).")
                scanner.wait_for_wake(cfg)
            log.info("Scanner awake — starting scan session.")
            _run_session(cfg, delimiter)
            # Session ended by idle while the scanner is still awake: wait for
            # it to sleep before re-arming, so we don't immediately re-poll.
            # But cap the wait — some ES-50 firmware goes idle without ever
            # disconnecting, and an unbounded wait here wedges the daemon deaf
            # to fed sheets. If it won't sleep, loop back and re-run a session
            # (present() is still true, so we skip wait_for_wake and scan again).
            if cfg.require_wake and scanner.present(cfg):
                log.info("Waiting for the scanner to sleep before re-arming.")
                if not scanner.wait_for_sleep(cfg, timeout=cfg.sleep_wait_timeout):
                    log.info(
                        "Scanner still present after %.0fs without sleeping — "
                        "re-arming a scan session so fed sheets aren't ignored.",
                        cfg.sleep_wait_timeout,
                    )
        except Exception:  # noqa: BLE001 - the daemon must survive anything
            # Back off well clear of the scanner's timing so we never hammer a
            # not-yet-ready or momentarily-wedged device into a worse state.
            log.exception("Unexpected error; backing off 10s.")
            time.sleep(10)


def _run_session(cfg: Config, delimiter: Delimiter) -> None:
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    _clear_work(cfg)

    pending: list[Path] = []
    page_index = 0
    last_page = time.monotonic()

    try:
        with scanner.ScannerSession(cfg) as session:
            while True:
                if not scanner.present(cfg):
                    log.info("Scanner disconnected (asleep).")
                    break

                result, image = session.get_page()

                if result is ScanResult.PAGE and image is not None:
                    page_index += 1
                    png = cfg.work_dir / f"page-{page_index:04d}.png"
                    try:
                        save_page_image(image, png, dpi=cfg.resolution)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Failed to save scanned sheet: %s", exc)
                        continue
                    pending.append(png)
                    last_page = time.monotonic()
                    log.info("Scanned sheet %d of current mail.", len(pending))
                    continue

                idle = time.monotonic() - last_page
                action = delimiter.poll(
                    Context(pending_pages=len(pending), idle_seconds=idle)
                )
                if action is Action.FINALIZE_EMAIL and pending:
                    _finalize(cfg, pending)
                    pending = []
                elif action is Action.END_SESSION:
                    if pending:
                        _finalize(cfg, pending)
                        pending = []
                    log.info("Session idle — stopping polling so the scanner can sleep.")
                    break

                time.sleep(cfg.poll_interval)
    finally:
        if pending:  # scanner vanished mid-mail — don't lose the pages
            _finalize(cfg, pending)


def _finalize(cfg: Config, pending: list[Path]) -> Path | None:
    valid = [p for p in pending if validate_image(p)]
    dropped = len(pending) - len(valid)
    if dropped:
        log.warning("%d scanned sheet(s) were corrupt and skipped — "
                    "please re-feed them.", dropped)
    if not valid:
        log.error("No valid pages to save; leaving files in %s", cfg.work_dir)
        return None

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out = unique_path(cfg.output_dir / f"{cfg.filename_prefix}_{ts}.pdf")
    try:
        images_to_pdf(valid, out)
    except Exception:  # noqa: BLE001
        log.exception("Failed to build PDF; pages kept in %s", cfg.work_dir)
        return None

    for p in pending:
        p.unlink(missing_ok=True)
    log.info("Saved mail: %d page(s) → %s", len(valid), out)
    return out


def _clear_work(cfg: Config) -> None:
    for leftover in cfg.work_dir.glob("page-*.png"):
        leftover.unlink(missing_ok=True)
