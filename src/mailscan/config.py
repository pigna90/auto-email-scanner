"""Configuration loading for mailscan.

Settings come from a TOML file. Search order (first match wins):
  1. $MAILSCAN_CONFIG
  2. ~/.config/mailscan/config.toml
  3. <repo>/config.toml   (the shipped defaults)

Anything not specified falls back to the dataclass defaults below.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    # --- scanner / SANE ---
    device: str = "epsonds"                # SANE device backend
    source: str = "ADF Front"              # sheet feeder
    mode: str = "Color"                    # Color | Gray | Lineart
    resolution: int = 300                  # 200 | 300 | 400 | 600
    usb_id: str = "04b8:016c"              # Epson ES-50 (vendor:product)

    # --- batching behaviour (the "timeout" delimiter) ---
    # After this many seconds with no new sheet, the current mail item is
    # finalized into one PDF. The next sheet starts a new mail item.
    email_timeout: float = 20.0
    # After this many seconds of total silence (nothing pending), stop polling
    # so the scanner is free to fall asleep. The next button-wake starts fresh.
    session_idle_timeout: float = 60.0
    # How often to check the feeder while a session is active.
    poll_interval: float = 2.0
    # After a session ends we stop touching USB and wait for the scanner to
    # drop off the bus (sleep). If it stays enumerated but idle for this many
    # seconds (some ES-50 firmware does this instead of disconnecting), give up
    # waiting and re-arm a scan session anyway, so a fed sheet is never ignored.
    sleep_wait_timeout: float = 120.0

    # --- wake behaviour ---
    # If True, when idle we wait (via udev) for the scanner to reconnect
    # (i.e. you press the button to wake it) before polling. This keeps the
    # scanner asleep and un-polled while unused. If False, we poll whenever the
    # scanner is present on the USB bus.
    require_wake: bool = True

    # --- output ---
    output_dir: Path = field(default_factory=lambda: Path.home() / "scans")
    work_dir: Path = field(
        default_factory=lambda: Path.home() / ".local/share/mailscan/work"
    )
    filename_prefix: str = "mail"

    # --- upload (rclone → Google Drive) ---
    # rclone "remote:path" the finished PDFs are moved to. Must match the
    # remote configured in ~/.config/rclone/rclone.conf.
    drive_remote: str = "MailScans:MailScans"
    # Skip PDFs younger than this many seconds — they may still be mid-write.
    upload_min_age: float = 60.0

    # --- Telegram notification (sent after a PDF lands on Drive) ---
    # Secrets: prefer the TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID environment
    # variables (env wins over these fields) so the token never has to live in
    # the checked-in config.toml. Leave blank to disable notifications.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- LLM classification + summary (one Claude call in the upload path) ---
    # Like the Telegram secrets, prefer the ANTHROPIC_API_KEY env var (it wins
    # over this field) so the key stays out of the checked-in config. Leave
    # blank to disable the LLM entirely — files then keep their timestamp name,
    # go to `default_category`, and the Telegram message carries no summary.
    anthropic_api_key: str = ""
    # Language the summary (shown inline in the Telegram message, inside an
    # expandable blockquote) is written in — any language name the model
    # understands, e.g. "Italian", "English", "German". Only affects the LLM's
    # output — all code/config text stays English.
    summary_language: str = "Italian"

    # --- automatic classification (one Claude call → title + category +
    #     summary), run in the upload path. Like the summary it is decoupled and
    #     best-effort: it never runs in the scanner daemon, and any failure falls
    #     back to the timestamp name / default category. See namer.py. ---
    # Claude vision model that reads the whole PDF (reuses anthropic_api_key
    # above). A blank key disables classification — files then keep their
    # `mail_<ts>.pdf` name and go to `default_category`.
    naming_claude_model: str = "claude-haiku-4-5"
    # The document categories the LLM must choose from, each mapped to the Drive
    # subfolder name and a description shown to the model to guide the choice.
    # The keys are also the folder names created under the Drive remote. "other"
    # is the catch-all / fallback. Override the whole set in config.toml under a
    # [categories] table.
    categories: dict = field(
        default_factory=lambda: {
            "medical": (
                "Health and medical matters: doctors, hospitals, prescriptions, "
                "lab results and medical reports, therapy, vaccinations — "
                "anything health-related."
            ),
            "insurance": (
                "Insurance policies, contributions and claims of any kind: "
                "health insurance (Krankenkasse/AOK), liability (Haftpflicht), "
                "car (KFZ), home/contents (Hausrat), life, legal protection."
            ),
            "finance": (
                "Banking and money: bank and account statements, the tax office "
                "and tax returns, invoices and bills to be paid, payment and "
                "transfer confirmations, salary and payslips, loans, investments."
            ),
            "household": (
                "Home and utilities: electricity, gas, water, heating, internet, "
                "telephone/mobile, rent and landlord correspondence, home "
                "services and repairs."
            ),
            "government": (
                "Official and state communications: city hall/Bürgeramt, tax "
                "authority, police, courts, residence and registration, "
                "pensions, the public broadcasting fee (Rundfunkbeitrag/ARD/ZDF)."
            ),
            "other": "Anything that does not clearly belong to a category above.",
        }
    )
    # Where a document goes when the model can't be made to pick a valid category
    # (must be one of the keys above).
    default_category: str = "other"
    # If the model returns a category that isn't one of the keys, re-ask this
    # many extra times before falling back to `default_category`.
    classify_max_retries: int = 3
    # Language the LLM writes the file name (title) in — it translates into this
    # from whatever language the document is in. English keeps names uniform
    # regardless of the (often German) source document.
    naming_language: str = "English"

    @classmethod
    def load(cls, explicit: str | os.PathLike | None = None) -> "Config":
        path = _find_config(explicit)
        data: dict = {}
        if path is not None:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        known = {f.name for f in fields(cls)}
        kwargs = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key in ("output_dir", "work_dir"):
                value = Path(os.path.expanduser(str(value)))
            kwargs[key] = value
        cfg = cls(**kwargs)
        # Secrets from the environment win over anything in the TOML, so the
        # bot token can be injected by systemd (EnvironmentFile=) and kept out
        # of the checked-in config.
        env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        env_chat = os.environ.get("TELEGRAM_CHAT_ID")
        env_key = os.environ.get("ANTHROPIC_API_KEY")
        if env_token:
            cfg.telegram_bot_token = env_token
        if env_chat:
            cfg.telegram_chat_id = env_chat
        if env_key:
            cfg.anthropic_api_key = env_key
        cfg._source_path = path  # type: ignore[attr-defined]
        return cfg

    @property
    def vid_pid(self) -> tuple[str, str]:
        vid, pid = self.usb_id.split(":")
        return vid.lower().removeprefix("0x"), pid.lower().removeprefix("0x")


def _find_config(explicit: str | os.PathLike | None) -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("MAILSCAN_CONFIG")
    if env:
        candidates.append(Path(env))
    candidates.append(Path.home() / ".config" / "mailscan" / "config.toml")
    candidates.append(Path(__file__).resolve().parents[2] / "config.toml")
    for c in candidates:
        if c and c.is_file():
            return c
    return None


def load_config(explicit: str | os.PathLike | None = None) -> Config:
    return Config.load(explicit)
