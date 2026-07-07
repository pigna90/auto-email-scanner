# mailscan

Headless batch scanning for the **Epson WorkForce ES-50** on a Raspberry Pi.

Feed sheets one at a time; mailscan auto-scans each one and groups them into
**one PDF per mail item**. No screen, no keyboard, no button-mashing on the
scanner — it runs as a background service on the Pi.

- **v1 (this repo):** mail items are delimited by an inactivity **timeout** —
  stop feeding for a bit and the current PDF is finalized; the next sheet
  starts a new one.
- **v2 (planned):** a **physical GPIO button** on the Pi makes the "this mail
  is done" boundary explicit instead of timed. The code already has the seam
  for it (see [Roadmap](#roadmap)).

---

## Contents
- [How it works](#how-it-works)
- [Hardware / OS](#hardware--os)
- [Part 1 — Installing the scanner driver (reproducible)](#part-1--installing-the-scanner-driver-reproducible)
- [Part 2 — The button-wakeup discovery](#part-2--the-button-wakeup-discovery)
- [Part 3 — Installing mailscan](#part-3--installing-mailscan)
- [Usage](#usage)
- [Configuration](#configuration)
- [Roadmap](#roadmap)
- [Known limitations](#known-limitations)

---

## How it works

```
😴 Scanner asleep  →  OFF the USB bus  →  Pi polls NOTHING (it stays asleep)
        │
🔘 You press the scanner button
        ▼
   Scanner wakes → reconnects to USB → udev fires "add" → mailscan starts a session
        │
📄 Feed sheets one at a time  →  each is auto-scanned and buffered
        │
   ⏱️  ~20s with no new sheet  →  buffered pages merged → mail_<timestamp>.pdf ✅
   ⏱️  ~60s of total silence   →  stop polling → scanner falls back asleep 🔁
```

The daemon is a small state machine (`src/mailscan/session.py`):

1. **Idle** — wait (via udev) for the scanner to reconnect. Nothing is polled,
   so the scanner sleeps undisturbed.
2. **Session** — while the scanner is on the bus, repeatedly attempt a scan:
   - a sheet scanned → buffer it as a page of the current mail item;
   - feeder empty for `email_timeout` → **finalize** the current mail into one PDF;
   - silence for `session_idle_timeout` → **end the session**, let it sleep.

The "when to finalize" logic lives behind a `Delimiter` interface
(`src/mailscan/triggers.py`) so the GPIO button can replace the timer later
without touching the loop.

---

## Hardware / OS

| | |
|---|---|
| Scanner | Epson WorkForce **ES-50**, USB id `04b8:016c`, ESC/I-2 protocol |
| Host | Raspberry Pi (arm64 / aarch64) |
| OS | Debian GNU/Linux 13 (trixie) / Raspberry Pi OS |
| Scan stack | SANE `epsonds` backend, driven in-process via **python-sane** |
| PDF assembly | `img2pdf` + `pillow` (built from scanned page images) |
| Python | 3.11+ (built on 3.13), managed with [`uv`](https://docs.astral.sh/uv/) |

---

## Part 1 — Installing the scanner driver (reproducible)

> One command does all of this: `sudo ./scripts/setup-permissions.sh`.
> The steps below are what that script does, and *why* — the ES-50 needs three
> non-obvious tweaks beyond a plain `apt install`.

### 1. Install SANE (runtime + dev headers)
```bash
sudo apt-get update
sudo apt-get install -y sane-utils libsane1 libsane-dev
```
`sane-utils` provides `scanimage` for manual verification; `libsane-dev` is
needed to build **python-sane**, the in-process binding mailscan scans with
(no shelling out to a CLI). The PDF is assembled in Python via `img2pdf`.

### 2. Tell the `epsonds` backend about the ES-50  ⚠️ non-obvious
Out of the box `scanimage -L` found **nothing** — the backend's built-in USB id
list didn't include `04b8:016c`, so it skipped the device during probing.
Fix: add an explicit line to `/etc/sane.d/epsonds.conf`:
```bash
echo 'usb 0x4b8 0x16c' | sudo tee -a /etc/sane.d/epsonds.conf
```

### 3. Fix USB permissions  ⚠️ non-obvious
SANE could see the device but couldn't open it (`Access denied`). The default
`uaccess` rule only grants the user physically logged in at the seat — no good
for a headless/SSH Pi. Add an explicit udev rule granting the scanner to a
`scanner` group:
```bash
sudo groupadd -f scanner
sudo usermod -aG scanner "$USER"
sudo tee /etc/udev/rules.d/65-epson-es50.rules >/dev/null <<'EOF'
ATTRS{idVendor}=="04b8", ATTRS{idProduct}=="016c", MODE="0664", GROUP="scanner", ENV{libsane_matched}="yes"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

### 4. Re-plug and verify
udev rules only apply to a *fresh* connection, so **unplug and replug the
scanner** (and re-login for the group to take effect). Then:
```bash
scanimage -L
# device `epsonds:libusb:003:005' is a Epson ES-50 ESC/I-2
```
A first test scan:
```bash
scanimage -d epsonds --source "ADF Front" --mode Color --resolution 300 \
  --format=pdf > ~/scan-test.pdf
```
> The harmless warning `Corrupt JPEG data: found marker 0xd9 instead of RSTn`
> shows up on most scans and does **not** affect the output.

---

## Part 2 — The button-wakeup discovery

The ES-50 has a single button. We wanted it to *start* a batch on a headless Pi.
Here's what we found by experiment — it's the basis for the whole wake design.

**The button is not a keyboard/HID device.** The scanner exposes two USB
interfaces, both *Vendor Specific Class*; it creates **no** `/dev/input` node.
So a press sends nothing to the OS directly, and SANE's `epsonds` backend
exposes **no** button sensor either. Reading the button "directly" is a dead end.

**But the scanner disconnects when it sleeps — and reconnects when woken.**
Watching `dmesg`/`udevadm monitor` while operating the scanner:

| Event | What Linux sees |
|---|---|
| Scanner falls asleep (idle) | `usb 3-1: USB disconnect` — it drops off the bus entirely |
| **Press button while asleep** | `usb 3-1: New USB device found … Epson ES-50` — it **reconnects** ✅ |
| Press button while **awake** | *nothing* |
| Press-and-hold while awake | *nothing* (no power-off gesture) |

**Conclusion:** the button can only ever signal **"start"** (via the wake →
USB *reconnect* → udev `add` event). It can **never** signal "stop". That's
exactly why mailscan uses the button-wake as the session trigger, and a
**timeout** (v1) or a **GPIO button** (v2) to delimit mail items.

Reproduce it yourself:
```bash
# leave this running, then sleep/wake the scanner and press its button:
udevadm monitor --udev --subsystem-match=usb
# or:
sudo dmesg -w | grep -i "usb 3-1"
```

> Power note: while a session is active mailscan polls the feeder, which keeps
> the scanner awake — but that's just an electronic "is paper there?" query; no
> rollers move. When idle, mailscan polls nothing and the scanner sleeps
> normally (it even drops off the USB bus). It is **not** always-on.

---

## Part 3 — Installing mailscan

Assuming Part 1 is done (`scanimage -L` lists the ES-50):

```bash
cd ~/mailscan
uv sync                     # creates .venv, installs pyudev + pypdf
uv run mailscan doctor      # sanity-check scanner + environment
```

Run it in the foreground to try it:
```bash
uv run mailscan run
# now press the scanner button and feed sheets
```

### Run as a service (recommended, headless)
```bash
sudo cp systemd/mailscan.service /etc/systemd/system/mailscan.service
# edit User=/paths in the unit if yours differ
sudo systemctl daemon-reload
sudo systemctl enable --now mailscan.service
journalctl -u mailscan -f          # watch it work
```
The unit sets `SupplementaryGroups=scanner`, so the service has scanner access
with no extra steps.

---

## Usage

Day-to-day, once the service is running:

1. **Press the scanner button** to wake it → a session starts.
2. **Feed the sheets** of one mail item, one at a time. Take your time —
   within a mail item you won't be split as long as gaps stay under
   `email_timeout`.
3. **Pause** (~`email_timeout`s). That mail is saved to `~/scans/mail_<timestamp>.pdf`.
4. Feed the next mail item's sheets → a new PDF. Repeat.
5. Walk away; after `session_idle_timeout` the scanner sleeps.

Copy results to your Mac:
```bash
scp 'alessandro.romano@192.168.1.205:~/scans/*.pdf' ~/Desktop/
```

CLI reference:
```bash
uv run mailscan run                 # the daemon
uv run mailscan scan-page out.pdf   # scan one sheet (test)
uv run mailscan doctor              # environment check
uv run mailscan -v run              # debug logging
```

Run the tests (no hardware needed):
```bash
uv run pytest
```

---

## Configuration

Defaults live in [`config.toml`](config.toml). Override by copying it to
`~/.config/mailscan/config.toml`, or point `$MAILSCAN_CONFIG` at your own file,
or pass `-c path/to/config.toml`.

| Key | Default | Meaning |
|---|---|---|
| `email_timeout` | `20.0` | seconds of no sheet → finalize current mail item |
| `session_idle_timeout` | `60.0` | seconds of silence → stop polling, let it sleep |
| `poll_interval` | `2.0` | how often the feeder is checked during a session |
| `require_wake` | `true` | stay silent while asleep; wake via button (udev) |
| `resolution` | `300` | 200 / 300 / 400 / 600 dpi |
| `mode` | `Color` | Color / Gray / Lineart |
| `output_dir` | `~/scans` | where finished PDFs land |

**Tuning the timeout:** raise `email_timeout` if you tend to pause mid-mail and
get split; lower it if consecutive mails get merged together.

---

## Roadmap

**v2 — physical GPIO button (the real fix for the timeout's fragility).**
A momentary button wired to a Pi GPIO pin becomes an explicit "this mail is
done" signal — no timing guesswork. The code is already shaped for it:

- `src/mailscan/triggers.py` defines the `Delimiter` interface and a
  commented `GpioDelimiter` sketch. Only that one class changes.
- `session.py` already asks the delimiter for `FINALIZE_EMAIL` / `END_SESSION`
  — it doesn't care whether the answer comes from a timer or a button.

Wiring plan: momentary switch between a GPIO pin (e.g. GPIO17) and GND;
`gpiozero.Button(17).when_pressed` sets a flag the `GpioDelimiter` reads.

---

## Known limitations

- **Timeout is a guess (v1).** Pause longer than `email_timeout` mid-mail and it
  splits; feed the next mail within `email_timeout` and they merge. This is the
  motivation for the v2 GPIO button.
- **Wake-transition window.** After a session ends, mailscan waits for the next
  udev `add`. If you feed a sheet in the brief moment after mailscan goes idle
  but before the scanner has actually slept, it may be missed — just press the
  button to wake it and re-feed.
- **JPEG warning.** `Corrupt JPEG data … RSTn` is cosmetic; output is fine.
- Tested only against the ES-50 (`04b8:016c`) on Debian 13 / RPi OS arm64.
