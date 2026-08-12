"""Shared fixtures for the border tests.

Test images are generated rather than checked in, because these tests assert
exact pixel counts and real camera files are never exactly 2:3 or 1:1 — a Fuji
"3:2" export is 4066x2711, which is 1.49982.

The gradient deliberately keeps every channel in [20, 200] so no pixel is
anywhere near white. That matters: the photo/border boundary is a hard edge,
and JPEG ringing around it bleeds a few levels into the white margin. A dark
fixture leaves a wide gap between "ringing" and "photo", so the measurement
below can separate them with a threshold.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageChops

from src.config import load_config

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Any pixel differing from white by more than this counts as photo, not ringing.
# Fixture pixels differ by >= 55; JPEG ringing at q92 is a handful of levels.
PHOTO_THRESHOLD = 40


def gradient(w: int, h: int) -> Image.Image:
    """A smooth non-white gradient at exactly w x h."""
    small = Image.new("RGB", (64, 64))
    small.putdata(
        [
            (20 + (180 * x) // 63, 30 + (150 * y) // 63, 200 - (150 * x) // 63)
            for y in range(64)
            for x in range(64)
        ]
    )
    return small.resize((w, h), Image.Resampling.BILINEAR)


@pytest.fixture(scope="session")
def cfg():
    return load_config().border


@pytest.fixture(scope="session")
def make_photo():
    """Write a gradient JPEG of the given size, return its path."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    def _make(name: str, w: int, h: int, **save_kwargs) -> Path:
        path = FIXTURE_DIR / name
        gradient(w, h).save(path, "JPEG", quality=95, **save_kwargs)
        return path

    return _make


def photo_bbox(path: Path, background: str = "#FFFFFF") -> tuple[int, int, int, int]:
    """Measure where the photo actually sits in a saved output file.

    Returns (left, top, right, bottom) with right/bottom exclusive, so the
    photo is (right-left) x (bottom-top) and the left margin is `left`.
    """
    with Image.open(path) as out:
        out = out.convert("RGB")
        diff = ImageChops.difference(out, Image.new("RGB", out.size, background))
        # Max across channels, not convert("L"): luminance would weight a
        # blue-only difference down to 11% and lose saturated edge pixels.
        r, g, b = diff.split()
        peak = ImageChops.lighter(ImageChops.lighter(r, g), b)
        return peak.point(lambda v: 255 if v > PHOTO_THRESHOLD else 0).getbbox()
