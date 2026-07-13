"""Tests for the hardware-free bits of the namer: prompt/schema building, name
sanitising, category validation, scan-date extraction and final-name assembly.

Nothing here touches the anthropic SDK — that is the untested seam, exercised
only against the real API (like scanner.py's hardware)."""

from pathlib import Path

from mailscan import namer


def test_sanitize_name_slugifies_and_strips_accents():
    assert namer.sanitize_name("Bolletta Enel — Maggio 2026") == "bolletta-enel-maggio-2026"
    assert namer.sanitize_name("  Ácçèntì!!  ") == "accenti"


def test_sanitize_name_caps_length_without_trailing_hyphen():
    slug = namer.sanitize_name("word " * 40, max_len=20)
    assert len(slug) <= 20
    assert not slug.endswith("-")


def test_sanitize_name_empty_when_nothing_usable():
    assert namer.sanitize_name("***") == ""
    assert namer.sanitize_name("") == ""


def test_scan_date_from_filename_timestamp():
    assert namer.scan_date_from(Path("mail_2026-07-13_101500.pdf")) == "2026-07-13"


def test_scan_date_from_mtime_when_no_timestamp(tmp_path):
    p = tmp_path / "scan.pdf"
    p.write_bytes(b"%PDF-1.4")
    # Falls back to the file's mtime, formatted as YYYY-MM-DD (10 chars).
    date = namer.scan_date_from(p)
    assert len(date) == 10 and date.count("-") == 2


def test_build_final_name_combines_date_and_slug():
    assert namer.build_final_name("bolletta-enel", "2026-07-13") == "2026-07-13_bolletta-enel"


def test_build_final_name_date_only_when_slug_empty():
    assert namer.build_final_name("", "2026-07-13") == "2026-07-13"


# --- combined classify (title + category + summary) helpers ---------------

CATEGORIES = {
    "medical": "Health and medical matters.",
    "insurance": "Insurance policies and claims.",
    "other": "Anything else.",
}


def test_classify_schema_enumerates_category_keys():
    schema = namer.classify_schema(CATEGORIES)
    assert schema["properties"]["category"]["enum"] == ["medical", "insurance", "other"]
    assert schema["required"] == ["title", "category", "summary"]


def test_classify_prompt_includes_keys_descriptions_and_languages():
    prompt = namer.build_classify_prompt(CATEGORIES, "English", "Italian")
    assert '"medical"' in prompt and "Health and medical matters." in prompt
    assert "English" in prompt   # title language
    assert "Italian" in prompt   # summary language


def test_validate_category_matches_case_insensitively():
    assert namer.validate_category("Medical", CATEGORIES) == "medical"
    assert namer.validate_category("INSURANCE", CATEGORIES) == "insurance"


def test_validate_category_rejects_unknown():
    assert namer.validate_category("taxes", CATEGORIES) is None
    assert namer.validate_category("", CATEGORIES) is None
    assert namer.validate_category(None, CATEGORIES) is None
