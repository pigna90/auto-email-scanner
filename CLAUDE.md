# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Headless batch scanner for the **Epson WorkForce ES-50** running as a background
service on a Raspberry Pi. Feed sheets one at a time; mailscan auto-scans each
and groups them into **one PDF per mail item**, where the boundary between mail
items is currently an inactivity timeout (v1). Runs against real USB scanner
hardware — most of the code exists to work around this specific device's quirks.

## Commands

```bash
uv sync                              # create .venv, install deps
uv run pytest                        # run the test suite (no hardware needed)
uv run pytest tests/test_merge.py::test_unique_path_avoids_clobber   # single test
uv run mailscan doctor               # check scanner + environment
uv run mailscan run                  # run the daemon in the foreground
uv run mailscan -v run               # daemon with debug logging
uv run mailscan scan-page out.pdf    # scan one sheet to a file (.pdf or .png)
sudo ./scripts/setup-permissions.sh  # one-time host setup (SANE, udev, group)
```

Tests import `pikepdf`, which is **not** declared in `[dependency-groups].dev`
(only `pytest` is). If `uv run pytest` fails on `ModuleNotFoundError: pikepdf`,
add it to the dev group rather than assuming a broken environment.

## The two hardware facts everything is built around

Read these before touching `scanner.py` or `session.py` — the design is
non-obvious without them (full write-up in README "Part 2"):

1. **The ES-50 disconnects from the USB bus when it sleeps** and reconnects when
   its button is pressed. So "is the scanner awake?" is answered by reading
   sysfs (`scanner.present()`), and "wait until the user wants to scan" means
   waiting for a udev `add` event (`scanner.wait_for_wake()`) — nothing is
   polled while it sleeps.
2. **The button emits no event while the scanner is already awake.** It can only
   ever signal *start*, never *stop*. That is why mail-item boundaries come from
   a timeout (v1) or a planned GPIO button (v2), not the scanner's own button.

A corollary that shows up throughout `scanner.py`: after any aborted/errored
scan you **must** call `cancel()` (see `_safe_cancel`), or the ES-50 stays
"busy" and eventually stops answering SANE entirely.

## Architecture

The daemon is a state machine. `run_daemon` (`session.py`) loops forever:
**idle** → `wait_for_wake` (block on udev) → `_run_session` → if still present,
`wait_for_sleep` (poll sysfs only, never touch USB) → repeat. It is written to
never die: any unexpected exception logs and backs off 10s rather than crashing.

Inside `_run_session`, each loop iteration calls `ScannerSession.get_page()`,
which returns one of three `ScanResult`s: `PAGE` (buffer it as a page of the
current mail), `NO_DOCS` (feeder empty — normal waiting state, detected by
matching substrings in SANE's error text), or `ERROR`. When no page arrives, the
loop asks the **delimiter** what to do.

**The `Delimiter` interface (`triggers.py`) is the key extension seam.** The
session loop only knows `poll(Context) -> Action` where Action is
`NONE` / `FINALIZE_EMAIL` / `END_SESSION`; it does not care whether the answer
comes from a timer (`TimeoutDelimiter`, v1) or the planned `GpioDelimiter` (v2,
sketched in comments). Adding the GPIO button should touch only that one class —
if a change to the batching logic requires editing `session.py`, reconsider.

Scanning is **in-process via python-sane** (`import sane`), not by shelling out
to `scanimage`. Each page comes back as a PIL image; PDFs are assembled in
`merge.py` with `img2pdf`. This is deliberate: the ES-50's CLI PDF output can be
silently truncated on a JPEG glitch, so mailscan rasterizes to PNG, validates
each page (`validate_image` via `Image.verify()`), and drops corrupt pages
before building the PDF itself.

Module map: `cli.py` (arg parsing + dispatch), `session.py` (the loop),
`scanner.py` (USB/SANE/wake), `triggers.py` (delimiters), `merge.py`
(image→PDF), `config.py` (TOML loading), `upload.py` (Drive upload + Telegram),
`bot.py` (Telegram summary-button poller), `summarize.py` (PDF→Italian summary
via Claude).

## Upload + Telegram notification (`upload.py`)

Uploading is **deliberately not part of the scanner daemon** — a Drive/network
hang must never stall scanning. `mailscan upload` (run by `mailscan-upload.timer`,
every 2 min) moves each finished PDF from `output_dir` to Drive and, per file,
posts an owner-only Drive link to Telegram. The transport is still **rclone**,
shelled out to one file at a time: `rclone moveto` (verifies hash, then deletes
local), then `rclone lsjson … --include /<name>` to read back the Drive file id,
from which `drive_view_link` builds `…/file/d/<id>/view`. Telegram is a plain
`urllib` POST to `sendMessage` (no new dependency). Everything after a successful
move is best-effort: a failed link lookup or Telegram post is logged, never
fatal — the PDF is already safely on Drive. Per-file upload failures leave the
file in place to retry next run. `upload_min_age` (default 60s) skips PDFs that
may still be mid-write.

Telegram secrets live in `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` **env vars**
(see `.env.example` → `~/.config/mailscan/telegram.env`, loaded
by the unit's `EnvironmentFile=`), which `Config.load` reads and **override** the
`telegram_*` TOML fields — so the token never lives in the checked-in
`config.toml`. Blank creds = notifications silently disabled, uploads still run.
The pure helpers (`drive_view_link`, `format_message`, file selection) are tested
in `tests/test_upload.py`; the rclone/urllib calls are the untested hardware-like
seam.

## On-demand summary button (`bot.py` + `summarize.py`)

A **separate, on-demand flow** bolted onto the upload notification — scanning and
uploading never call an LLM. When `upload.py` posts a PDF's Drive link, it also
attaches an inline **"🧠 Summarize"** button whose `callback_data` is
`sum:<drive_file_id>` (only if `anthropic_api_key` is set). Nothing happens until
the user taps it.

`mailscan bot` (run by `mailscan-bot.timer`, every 30s) polls Telegram
`getUpdates` for those taps. The button's `callback_data` is `sum:<pdf_name>`
(the **name**, not the Drive id) so the bot can find a local copy without a Drive
call. Per tap it: acks the button, loads the PDF via `_load_pdf` — **local cache
first, Drive `rclone cat` fallback** — sends it to Claude as a base64 `document`
block (`summarize.py` — the PDFs are scanned *images*, so we use a vision model,
**no OCR step**), and posts the summary as a reply. The summary is three fixed
sections — **Summary / Actions / Critical** — enforced by `build_prompt()`. **All
code/config text is English**; only the LLM's output language is configurable,
via `summary_language` (default `"Italian"`), which `build_prompt()` interpolates
into the instruction.

The **local cache** is why the button carries the name: `upload.py` `copy2`s each
PDF into `cache_dir` (default `~/.local/share/mailscan/cache`, **outside**
`output_dir` so it's never re-uploaded) *before* the `rclone moveto`, then prunes
to the newest `local_cache_size` (default 10). The cache is pure best-effort — if
it fails, upload is unaffected and the bot just falls back to Drive. The upload
path's verify-then-delete `moveto` is untouched.

Same philosophy as the uploader: **best-effort, timer-driven, never stalls
scanning** — an Anthropic/Drive hang can't touch the daemon. Everything after the
tap is caught-and-logged; a failure sends a short error reply, never crashes. The
getUpdates offset is persisted in `~/.local/share/mailscan/bot-offset` so taps
aren't reprocessed across runs. Model is `summary_model` (default
`claude-haiku-4-5` — cheapest vision model, plenty for a few pages).

Secrets: `ANTHROPIC_API_KEY` lives in the same `.env` (`EnvironmentFile=`) as the
Telegram creds and **overrides** the `anthropic_api_key` TOML field, so no key is
in the checked-in config. Blank key = the button is omitted; uploads still run.
The Anthropic call (via the `anthropic` SDK) and the rclone/urllib calls are the
untested seam; the pure helpers (`build_pdf_message`, `parse_summary_callback`,
offset I/O, `format_summary`, `build_summary_keyboard`) are tested.

## Config

`load_config` searches, first match wins: `-c`/explicit path → `$MAILSCAN_CONFIG`
→ `~/.config/mailscan/config.toml` → repo `config.toml` (shipped defaults).
Unknown TOML keys are ignored; only fields on the `Config` dataclass are read.
Note the dataclass defaults (`config.py`) and the shipped `config.toml` differ
(e.g. `email_timeout` 20.0 vs 8.0) — the TOML wins when present, so edit
`config.toml` for behavior changes, not the dataclass.

## Scanner-group access (why `sg` re-exec exists)

The udev rule grants the ES-50 to the `scanner` group. A login shell only picks
up group membership after re-login, so for ad-hoc `uv run mailscan ...` invocations
`cli._ensure_scanner_group()` transparently re-execs via `sg scanner` (guarded by
`MAILSCAN_REEXEC` to avoid a loop). The systemd unit sets
`SupplementaryGroups=scanner` and so skips this entirely.

## Testing constraint

Tests cover only the hardware-free path (`merge.py` and helpers). Anything in
`scanner.py`, `session.py`, or the wake logic can only be exercised against the
physical ES-50 on the Pi — there is no scanner mock. Keep hardware-touching code
behind the interfaces above so the testable core stays testable.
