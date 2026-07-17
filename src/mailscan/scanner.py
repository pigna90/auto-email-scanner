"""Talking to the Epson ES-50 in-process via python-sane (no subprocess).

Two facts about this scanner drive the design (see README "Discoveries"):
  * When it sleeps it DISCONNECTS from the USB bus; pressing the button wakes
    it and it RECONNECTS -> udev fires an "add" event we can wait on.
  * Its button produces NO event while it is already awake, so it can only
    signal "start", never "stop".

Scanning goes through SANE's `epsonds` backend either way — that's what speaks
to the ES-50 — but we drive it from Python and get each page back as a PIL
image, so we build the PDF ourselves instead of trusting a CLI's output.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

import sane
from PIL import Image

from .config import Config

log = logging.getLogger("mailscan")

# Substrings SANE uses to mean "feeder is empty" — a normal waiting state.
_NO_DOCS_MARKERS = ("out of documents", "no documents", "no more documents")

_sane_ready = False


class ScannerWedged(RuntimeError):
    """The scanner is on the USB bus but unreachable by SANE even after a fresh
    re-init. This is the ES-50's hung-firmware state: no software/bus reset
    clears it, only a physical power cycle (unplug/replug the USB cable). Raised
    so the daemon can escalate (alert the owner) instead of silently retrying —
    distinct from a plain "asleep/disconnected", which is a normal waiting state.
    """


class ScanResult(Enum):
    PAGE = "page"          # a sheet was scanned
    NO_DOCS = "no_docs"    # feeder empty (keep waiting)
    ERROR = "error"        # something went wrong


def _ensure_sane() -> None:
    global _sane_ready
    if not _sane_ready:
        sane.init()
        _sane_ready = True


def _reset_sane() -> None:
    """Tear libsane down so the next _ensure_sane() re-enumerates the USB bus.

    SANE snapshots the USB device list at sane.init() and never refreshes it on
    its own. After the ES-50 is physically unplugged and replugged it comes back
    with a NEW bus/device number, which the stale enumeration can't see — so the
    long-lived daemon keeps failing to find a scanner that a fresh `scanimage`
    process (with its own init) finds fine. Re-initialising is the only way to
    pick it back up without restarting the whole daemon.
    """
    global _sane_ready
    try:
        sane.exit()
    except Exception:  # noqa: BLE001 - tearing down an already-broken lib is fine
        pass
    _sane_ready = False


def present(cfg: Config) -> bool:
    """True if the scanner is on the USB bus (i.e. awake). Reads sysfs."""
    vid, pid = cfg.vid_pid
    for dev in Path("/sys/bus/usb/devices").glob("*"):
        try:
            dv = (dev / "idVendor").read_text().strip().lower()
            dp = (dev / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if dv == vid and dp == pid:
            return True
    return False


def find_device(cfg: Config) -> Optional[str]:
    """Return the current SANE device name for the scanner, e.g.
    'epsonds:libusb:003:006' (the bus/dev part changes on every wake)."""
    _ensure_sane()
    for name, _vendor, _model, _type in sane.get_devices():
        if name.startswith(cfg.device):
            return name
    return None


class ScannerSession:
    """An open SANE handle for one wake-session.

    Use as a context manager; call :meth:`get_page` once per sheet.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._dev = None

    def __enter__(self) -> "ScannerSession":
        name = find_device(self.cfg)
        if not name and present(self.cfg):
            # sysfs says the scanner is on the USB bus, but SANE can't see it:
            # its device enumeration is stale (typically an unplug/replug gave
            # the scanner a new bus/device number). Re-initialising libsane is
            # the only way to refresh that list — do it once, then look again.
            log.info("Scanner on the USB bus but invisible to SANE — "
                     "re-initialising SANE to recover.")
            _reset_sane()
            name = find_device(self.cfg)
            if not name:
                # Still invisible after a fresh init while sysfs says it's on the
                # bus: the firmware is hung (see ScannerWedged). No amount of
                # retrying here clears that — only a power cycle does.
                raise ScannerWedged(
                    "scanner on the USB bus but unreachable by SANE — "
                    "hung firmware; needs a physical unplug/replug"
                )
        if not name:
            raise RuntimeError("scanner not found by SANE (asleep or disconnected?)")
        self._dev = sane.open(name)
        # Clear any engine state a previous aborted/wedged session may have left
        # behind before we issue the first start() — a lingering "busy" is what
        # eventually stops the ES-50 answering SANE at all (see _safe_cancel).
        self._safe_cancel()
        # Best-effort parameter setup; not all firmwares expose every option.
        for attr, value in (("source", self.cfg.source),
                            ("mode", self.cfg.mode),
                            ("resolution", self.cfg.resolution)):
            try:
                setattr(self._dev, attr, value)
            except Exception as exc:  # noqa: BLE001 - option may be absent
                log.debug("could not set %s=%r: %s", attr, value, exc)
        return self

    def _safe_cancel(self) -> None:
        """Reset the scan engine's state. Safe no-op if nothing is in progress.

        Skipping this after an aborted/errored scan is what wedges the ES-50:
        it stays 'busy' and eventually stops answering SANE entirely.
        """
        try:
            if self._dev is not None:
                self._dev.cancel()
        except Exception:  # noqa: BLE001
            pass

    def get_page(self) -> Tuple[ScanResult, Optional[Image.Image]]:
        """Attempt to acquire one sheet.

        Returns (PAGE, image) if a sheet fed through, (NO_DOCS, None) if the
        feeder is empty, or (ERROR, None) on any other failure.
        """
        if self._dev is None:
            return ScanResult.ERROR, None
        try:
            self._dev.start()
            image = self._dev.snap()
        except Exception as exc:  # noqa: BLE001 - SANE raises a bare error type
            self._safe_cancel()  # always clear engine state after a failed scan
            msg = str(exc).lower()
            if any(marker in msg for marker in _NO_DOCS_MARKERS):
                return ScanResult.NO_DOCS, None
            log.warning("scan error: %s", exc)
            return ScanResult.ERROR, None
        return ScanResult.PAGE, image

    def close(self) -> None:
        if self._dev is not None:
            self._safe_cancel()
            try:
                self._dev.close()
            except Exception:  # noqa: BLE001
                pass
            self._dev = None

    def __exit__(self, *exc) -> None:
        self.close()


def wait_for_wake(cfg: Config) -> None:
    """Block until the scanner is on the USB bus (awake).

    If it is already present, returns at once — it's awake, so scan now. Only
    when it's genuinely asleep do we wait (via udev) for the button-press
    reconnect, so nothing is polled while it sleeps.
    """
    if present(cfg):
        return

    import pyudev

    vid, pid = cfg.vid_pid
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="usb")
    monitor.start()

    for device in iter(monitor.poll, None):
        if device.action != "add":
            continue
        dv = (device.get("ID_VENDOR_ID") or "").lower()
        dp = (device.get("ID_MODEL_ID") or "").lower()
        if not dv:  # some events lack properties; fall back to sysfs attrs
            dv = (device.attributes.get("idVendor") or b"").decode().lower()
            dp = (device.attributes.get("idProduct") or b"").decode().lower()
        if dv == vid and dp == pid:
            return


def wait_for_sleep(
    cfg: Config, timeout: float | None = None, poll_interval: float = 3.0
) -> bool:
    """Block until the scanner drops off the USB bus (sleeps on its own timer),
    or until `timeout` seconds pass. Returns True if it slept, False if it timed
    out while still present.

    Only reads sysfs — never touches the scanner over USB — so it does not keep
    it awake. Used after a session ends so we let it sleep before re-arming.

    The timeout matters because some ES-50 firmware goes idle *without*
    disconnecting: without a cap this loops forever and the daemon goes deaf to
    fed sheets (see run_daemon for how the caller recovers).
    """
    import time

    deadline = None if timeout is None else time.monotonic() + timeout
    while present(cfg):
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)
    return True
