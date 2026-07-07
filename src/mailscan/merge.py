"""Turn scanned page images into one PDF per mail item.

We assemble the PDF ourselves (via img2pdf) from complete PNG rasters, instead
of trusting a scanner CLI's PDF output — which on the ES-50 can be silently
truncated when its JPEG transfer glitches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import img2pdf
from PIL import Image


def save_page_image(image: Image.Image, dest: Path, dpi: int = 300) -> Path:
    """Save a scanned page as a lossless PNG with correct DPI metadata."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if image.mode not in ("RGB", "L", "1"):
        image = image.convert("RGB")
    image.save(dest, format="PNG", dpi=(dpi, dpi))
    return dest


def validate_image(path: Path) -> bool:
    """True if *path* is a fully readable image (not truncated/corrupt)."""
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:  # noqa: BLE001
        return False


def images_to_pdf(pages: Iterable[Path], out_path: Path) -> Path:
    """Combine page images (PNGs) into a single PDF at *out_path*."""
    paths = [str(p) for p in pages]
    if not paths:
        raise ValueError("no pages to merge")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(img2pdf.convert(paths))
    return out_path


def unique_path(path: Path) -> Path:
    """Return *path*, or path with a -2, -3… suffix if it already exists."""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    i = 2
    while True:
        candidate = parent / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1
