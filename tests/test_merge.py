"""Tests for image→PDF assembly and helpers — no scanner hardware required."""

from pathlib import Path

import pikepdf
import pytest
from PIL import Image

from mailscan.merge import (
    images_to_pdf,
    save_page_image,
    unique_path,
    validate_image,
)


def _png(path: Path, color=(255, 255, 255)) -> Path:
    Image.new("RGB", (200, 300), color).save(path, "PNG")
    return path


def test_images_to_pdf_concatenates_pages(tmp_path):
    pages = [_png(tmp_path / f"p{i}.png") for i in range(3)]
    out = images_to_pdf(pages, tmp_path / "out.pdf")
    assert out.read_bytes()[:5] == b"%PDF-"
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 3


def test_images_to_pdf_rejects_empty(tmp_path):
    with pytest.raises(ValueError):
        images_to_pdf([], tmp_path / "out.pdf")


def test_validate_image_detects_corruption(tmp_path):
    assert validate_image(_png(tmp_path / "good.png"))
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n truncated garbage")
    assert not validate_image(bad)


def test_save_page_image_roundtrips(tmp_path):
    out = save_page_image(Image.new("RGB", (100, 100)), tmp_path / "pg.png", dpi=300)
    assert out.exists() and validate_image(out)


def test_unique_path_avoids_clobber(tmp_path):
    first = tmp_path / "mail.pdf"
    first.write_bytes(b"x")
    assert unique_path(first) == tmp_path / "mail-2.pdf"
    (tmp_path / "mail-2.pdf").write_bytes(b"x")
    assert unique_path(first) == tmp_path / "mail-3.pdf"
