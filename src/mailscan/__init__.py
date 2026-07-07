"""mailscan — headless batch scanning for the Epson ES-50.

Auto-scans each sheet fed into the scanner and groups them into one PDF per
"mail item", delimited by an inactivity timeout (v1) or — later — a physical
GPIO button. See README.md for the full story and setup.
"""

__version__ = "0.1.0"
