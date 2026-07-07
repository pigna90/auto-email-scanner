#!/usr/bin/env bash
# Reproducible one-off host setup for the Epson ES-50 + SANE on Debian/RPi OS.
# Idempotent: safe to re-run. Run with sudo-capable user.
set -euo pipefail

VENDOR=04b8
PRODUCT=016c
USER_NAME="${SUDO_USER:-$USER}"

echo ">> Installing SANE (runtime + dev headers for the python binding)..."
sudo apt-get update -qq
# sane-utils gives `scanimage` for manual verification; libsane-dev is needed
# to build the python-sane binding that mailscan actually scans with.
sudo apt-get install -y sane-utils libsane1 libsane-dev

echo ">> Creating 'scanner' group and adding '$USER_NAME'..."
sudo groupadd -f scanner
sudo usermod -aG scanner "$USER_NAME"

echo ">> Installing udev rule (grants the ES-50 to group 'scanner')..."
sudo tee /etc/udev/rules.d/65-epson-es50.rules >/dev/null <<EOF
# Epson ES-50 — grant access to the 'scanner' group for SANE.
ATTRS{idVendor}=="${VENDOR}", ATTRS{idProduct}=="${PRODUCT}", MODE="0664", GROUP="scanner", ENV{libsane_matched}="yes"
EOF

echo ">> Telling the epsonds backend about this USB id..."
CONF=/etc/sane.d/epsonds.conf
if ! grep -q "0x4b8 0x16c" "$CONF" 2>/dev/null; then
  echo "usb 0x4b8 0x16c" | sudo tee -a "$CONF" >/dev/null
fi

echo ">> Reloading udev..."
sudo udevadm control --reload-rules
sudo udevadm trigger

cat <<EOF

Done. IMPORTANT: unplug and replug the scanner (or re-login) so the new group
and udev rule take effect. Then verify:

    scanimage -L        # should list: Epson ES-50 ESC/I-2

EOF
