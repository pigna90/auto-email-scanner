"""The scan daemon: wait for wake, scan sheets, group them into PDFs.

Designed to never die: a bad page or a scanner hiccup is logged and skipped,
and any unexpected error drops back to the idle/wait state instead of crashing.
"""

from __future__ import annotations

import logging
import signal
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


def _install_shutdown_handlers() -> None:
    """Turn SIGTERM/SIGINT into a clean SystemExit so an open scan session is
    torn down properly on `systemctl stop`/restart.

    Without this, systemd's SIGTERM kills the process where it stands — possibly
    mid-scan with the SANE handle still open — which can leave the ES-50 stuck
    'busy' and unreachable (exactly the wedge this daemon otherwise has to alert
    about). Raising SystemExit instead unwinds through the `with ScannerSession`
    block, so close() → cancel() runs and any buffered pages are finalized before
    we exit. SystemExit is a BaseException, so the loop's `except Exception` does
    not swallow it — it propagates out and the process exits 0 (a clean stop)."""
    def _handler(signum, _frame):
        log.info("Received signal %d — shutting down; closing any open scan "
                 "session cleanly.", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def run_daemon(cfg: Config, delimiter: Delimiter | None = None) -> None:
    """Top-level loop: idle → wait for wake → run a session → repeat forever."""
    delimiter = delimiter or build_delimiter(cfg)
    _install_shutdown_handlers()
    log.info("mailscan started. Output dir: %s", cfg.output_dir)
    # A hung-firmware wedge (ScannerWedged) can't be cleared in software — only a
    # physical unplug/replug fixes it. Rather than re-arm a dead session forever
    # in silence (which once hid the problem for ~50 min), we alert the owner
    # once per episode. Confirm it twice first so a transient enumeration lag at
    # wake never pings; reset both once any session actually opens the scanner.
    wedged_streak = 0
    wedged_alerted = False
    # Consecutive sessions that scanned nothing. Brisk re-arming keeps the scanner
    # awake and (see max_idle_rearms) eventually wedges it, so once this passes the
    # bound we stop poking and let it sleep. Any fed sheet resets it to 0.
    empty_streak = 0
    while True:
        try:
            if not scanner.present(cfg):
                if cfg.require_wake:
                    log.info("Idle — waiting for the scanner to wake "
                             "(press its button / feed a sheet).")
                scanner.wait_for_wake(cfg)
                empty_streak = 0  # a fresh wake — treat as active again
            log.info("Scanner awake — starting scan session.")
            pages = _run_session(cfg, delimiter)
            wedged_streak = 0
            wedged_alerted = False
            empty_streak = 0 if pages else empty_streak + 1

            # Session ended while the scanner is still awake. We must wait for it
            # to sleep before re-arming, but *how* we wait matters: repeatedly
            # re-arming a polling session keeps poking the scanner over USB, so it
            # never idles long enough to sleep and reset — which is what wedges it.
            if cfg.require_wake and scanner.present(cfg):
                if empty_streak > cfg.max_idle_rearms:
                    # Nothing has been fed for several sessions. Back off and leave
                    # the scanner alone so it can idle, sleep, and reset. Wait
                    # patiently (sysfs only, no USB) for it to drop off the bus; if
                    # it slept, the next wake is a real button-press and we start
                    # fresh. If it stubbornly won't disconnect, fall through and
                    # re-arm just once to catch a late-fed sheet, then come back
                    # here — far gentler than the old poll-every-2s-forever loop.
                    log.info(
                        "No sheets fed across %d sessions — leaving the scanner "
                        "alone so it can sleep (avoids the wedge from constant "
                        "polling).", empty_streak,
                    )
                    if scanner.wait_for_sleep(
                        cfg, timeout=cfg.idle_sleep_wait_timeout
                    ):
                        empty_streak = 0
                else:
                    # Recent activity: re-arm briskly so back-to-back mail items
                    # aren't missed. Cap the wait so a non-disconnecting scanner
                    # still gets re-armed rather than blocking here forever.
                    log.info("Waiting for the scanner to sleep before re-arming.")
                    if not scanner.wait_for_sleep(
                        cfg, timeout=cfg.sleep_wait_timeout
                    ):
                        log.info(
                            "Scanner still present after %.0fs without sleeping — "
                            "re-arming a scan session so fed sheets aren't ignored.",
                            cfg.sleep_wait_timeout,
                        )
        except scanner.ScannerWedged:
            wedged_streak += 1
            log.warning(
                "Scanner wedged — on the USB bus but unreachable by SANE "
                "(attempt %d). A power cycle (unplug/replug the USB cable) is "
                "needed.", wedged_streak,
            )
            if wedged_streak < 2:
                # Confirm it's a genuine wedge, not a transient enumeration lag
                # at wake, before escalating. Brief pause, then retry once.
                time.sleep(5)
                continue
            if not wedged_alerted:
                _alert_wedged(cfg)
                wedged_alerted = True
            # Confirmed hung firmware: only a physical replug clears it. Probing
            # find_device() again is futile AND leaks file descriptors through the
            # SANE re-init each attempt — enough to crash the daemon within
            # minutes (systemd then restarts it, re-alerting on a loop). So stop
            # probing and wait quietly — sysfs only, no USB, no leak — for the
            # scanner to drop off the bus (the unplug). The loop's wait_for_wake
            # then catches the replug and a fresh session recovers.
            log.info("Pausing until the scanner is unplugged — only a power "
                     "cycle clears this state.")
            scanner.wait_for_sleep(cfg, timeout=None)
        except Exception:  # noqa: BLE001 - the daemon must survive anything
            # Back off well clear of the scanner's timing so we never hammer a
            # not-yet-ready or momentarily-wedged device into a worse state.
            log.exception("Unexpected error; backing off 10s.")
            time.sleep(10)


def _alert_wedged(cfg: Config) -> None:
    """Best-effort Telegram ping that the scanner needs a physical power cycle.

    Reuses the uploader's Telegram sender. Silent no-op when Telegram isn't
    configured, and never raises — an alert failure must not disturb the loop."""
    token, chat = cfg.telegram_bot_token, cfg.telegram_chat_id
    if not (token and chat):
        log.info("Telegram not configured; cannot alert about the wedged scanner.")
        return
    try:
        from .upload import send_telegram

        send_telegram(
            token, chat,
            "⚠️ <b>mailscan</b>: the scanner is powered on but has "
            "stopped responding (hung firmware). Please <b>unplug its USB "
            "cable, wait ~5s, and plug it back in</b> — then re-feed your "
            "document. Scanning is paused until then.",
        )
        log.info("Alerted Telegram: scanner wedged, needs a power cycle.")
    except Exception:  # noqa: BLE001 - a failed alert must not fail the daemon
        log.exception("Failed to send scanner-wedged Telegram alert.")


def _run_session(cfg: Config, delimiter: Delimiter) -> int:
    """Run one wake-session; return the number of sheets fed (0 if none). The
    caller uses that count to decide whether to keep re-arming or let the scanner
    sleep (see run_daemon / max_idle_rearms)."""
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

    return page_index


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
