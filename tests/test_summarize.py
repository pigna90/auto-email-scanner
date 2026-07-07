"""Tests for the hardware-free bits of the summarizer: request payload shape.

The Anthropic API call (summarize_pdf) is the untested seam, exercised only
against the real key on the Pi. build_pdf_message is pure and does not import
`anthropic`, so these run without the SDK installed."""

import base64

from mailscan import summarize


def test_build_pdf_message_wraps_pdf_as_base64_document():
    pdf = b"%PDF-1.4 hello"
    messages = summarize.build_pdf_message(pdf, prompt="Summarize.")

    assert len(messages) == 1
    content = messages[0]["content"]
    doc, text = content[0], content[1]

    assert doc["type"] == "document"
    assert doc["source"]["media_type"] == "application/pdf"
    assert base64.standard_b64decode(doc["source"]["data"]) == pdf
    # Document precedes the instruction (recommended ordering).
    assert text["type"] == "text"
    assert text["text"] == "Summarize."


def test_prompt_is_english_with_three_sections_in_chosen_language():
    prompt = summarize.build_prompt("Italian")
    # Instruction text itself is English; only the output language varies.
    for section in ("Summary", "Actions", "Critical"):
        assert section in prompt
    assert "Italian" in prompt


def test_prompt_language_is_configurable():
    assert "German" in summarize.build_prompt("German")
    assert "Italian" not in summarize.build_prompt("German")
