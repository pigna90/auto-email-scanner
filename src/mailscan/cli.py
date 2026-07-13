"""Command line entry point: `mailscan run | scan-page | doctor`."""

from __future__ import annotations

import argparse
import grp
import logging
import os
import shlex
import sys
import time
from pathlib import Path

from .config import Config, load_config
from . import namer, scanner, session, upload
from .merge import images_to_pdf, save_page_image
from .scanner import ScanResult

log = logging.getLogger("mailscan")


def _have_scanner_access() -> bool | None:
    """True if this process can open the scanner (member of, or running as,
    group `scanner`). None if there is no `scanner` group at all."""
    try:
        gid = grp.getgrnam("scanner").gr_gid
    except KeyError:
        return None
    return gid in os.getgroups() or gid in (os.getgid(), os.getegid())


def _ensure_scanner_group() -> None:
    """Re-exec under the `scanner` group if we're not already in it.

    The udev rule grants the ES-50 to group `scanner`. A login shell only picks
    that up after re-login, so for ad-hoc runs we transparently re-exec via
    `sg scanner`. systemd sets SupplementaryGroups=scanner and skips this.
    """
    if os.environ.get("MAILSCAN_REEXEC") or _have_scanner_access() is not False:
        return
    os.environ["MAILSCAN_REEXEC"] = "1"
    os.execvpe("sg", ["sg", "scanner", "-c", shlex.join(sys.argv)], os.environ)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # These libraries are extremely chatty at DEBUG; keep our -v readable.
    for noisy in ("PIL", "img2pdf", "PIL.PngImagePlugin"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    _ensure_scanner_group()
    session.run_daemon(cfg)
    return 0


def cmd_scan_page(cfg: Config, args: argparse.Namespace) -> int:
    _ensure_scanner_group()
    out = Path(args.output).expanduser()
    with scanner.ScannerSession(cfg) as sess:
        result, image = sess.get_page()
    if result is ScanResult.NO_DOCS:
        print("No paper in the feeder (feed a sheet and try again).", file=sys.stderr)
        return 2
    if result is not ScanResult.PAGE or image is None:
        print("Scan failed.", file=sys.stderr)
        return 1
    if out.suffix.lower() == ".png":
        save_page_image(image, out, dpi=cfg.resolution)
    else:
        tmp = cfg.work_dir / "_scan_page.png"
        save_page_image(image, tmp, dpi=cfg.resolution)
        images_to_pdf([tmp], out)
        tmp.unlink(missing_ok=True)
    print(f"OK  → {out}  ({out.stat().st_size} bytes)")
    return 0


def cmd_upload(cfg: Config, args: argparse.Namespace) -> int:
    # No scanner access needed — this only touches the filesystem, rclone and
    # the network, so we skip the `sg scanner` re-exec.
    n = upload.upload_pending(cfg)
    log.info("Upload run complete: %d file(s).", n)
    return 0


def cmd_name(cfg: Config, args: argparse.Namespace) -> int:
    # Test the combined title + category + summary Claude call on one PDF,
    # without touching Drive.
    pdf = Path(args.pdf).expanduser()
    if not pdf.is_file():
        print(f"No such file: {pdf}", file=sys.stderr)
        return 2
    if not cfg.anthropic_api_key:
        print("No ANTHROPIC_API_KEY / anthropic_api_key configured.", file=sys.stderr)
        return 2
    print(f"Classifying {pdf.name} via Claude ({cfg.naming_claude_model})…")
    print(f"  categories: {', '.join(cfg.categories)}")
    t0 = time.monotonic()
    result = namer.classify_document(cfg, pdf)
    dt = time.monotonic() - t0
    if result is None:
        print(f"  FAILED (see log above)   [{dt:.1f}s]", file=sys.stderr)
        return 1
    print(f"  title   : {result.stem}.pdf")
    print(f"  category: {result.category}")
    print(f"  → {result.category}/{result.stem}.pdf   [{dt:.1f}s]")
    print("  summary :")
    for line in result.summary.splitlines():
        print(f"    {line}")
    return 0


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    print("mailscan doctor")
    print(f"  config file        : {getattr(cfg, '_source_path', None) or '(defaults)'}")
    print(f"  scanner USB id      : {cfg.usb_id}")
    print(f"  scanner on bus      : {'yes (awake)' if scanner.present(cfg) else 'no (asleep/off)'}")
    in_group = _have_scanner_access()
    print(f"  in 'scanner' group  : "
          f"{'yes' if in_group else ('no (will re-exec via sg)' if in_group is False else 'no group')}")
    if in_group:
        try:
            name = scanner.find_device(cfg)
            print(f"  SANE device         : {name or 'NOT FOUND'}")
        except Exception as exc:  # noqa: BLE001
            print(f"  SANE device         : error ({exc})")
    else:
        print("  SANE device         : (skipped — not in scanner group; run via `sg scanner`)")
    print(f"  output dir          : {cfg.output_dir}")
    print(f"  email timeout       : {cfg.email_timeout}s")
    print(f"  session idle timeout: {cfg.session_idle_timeout}s")
    print(f"  require wake (udev) : {cfg.require_wake}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailscan", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("-c", "--config", help="path to a config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="run the scanning daemon")
    sp = sub.add_parser("scan-page", help="scan a single sheet to a file (test)")
    sp.add_argument("output", help="destination .pdf (or .png) path")
    sub.add_parser("doctor", help="check scanner + environment")
    sub.add_parser("upload", help="move finished PDFs to Drive + notify Telegram")
    npar = sub.add_parser("name", help="test the LLM naming/foldering on one PDF")
    npar.add_argument("pdf", help="path to a PDF to name")
    return p


_DISPATCH = {
    "run": cmd_run,
    "scan-page": cmd_scan_page,
    "doctor": cmd_doctor,
    "upload": cmd_upload,
    "name": cmd_name,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_config(args.config)
    return _DISPATCH[args.cmd](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
