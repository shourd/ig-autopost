"""EXIF date extraction for caption suffixes.

The month name comes from a hardcoded English list, never strftime("%b") —
this machine's locale is Dutch and would produce "mrt" where the account's
style needs "Mar".
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PIL import Image

# Hardcoded on purpose. Do not replace with strftime("%b") — see module docstring.
MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

# Tried in this order. DateTimeOriginal is when the shutter fired; the other two
# are progressively less trustworthy, and DateTime in particular is rewritten by
# Lightroom on export.
DATE_TAGS = (
    ("DateTimeOriginal", 0x9003, True),   # (name, tag, lives in the Exif sub-IFD)
    ("DateTime", 0x0132, False),
    ("DateTimeDigitized", 0x9004, True),  # a.k.a. CreateDate
)

EXIF_IFD = 0x8769
UNKNOWN_SUFFIX = " (?, ?)"


def read_capture_date(path: Path | str) -> tuple[datetime | None, str | None]:
    """Return (capture datetime, which EXIF tag it came from).

    (None, None) means no usable date — the caller must flag the photo rather
    than substituting today's date or the file mtime.
    """
    with Image.open(path) as im:
        exif = im.getexif()
        if not exif:
            return None, None
        sub_ifd = exif.get_ifd(EXIF_IFD)

    for name, tag, in_sub_ifd in DATE_TAGS:
        raw = (sub_ifd if in_sub_ifd else exif).get(tag)
        parsed = _parse(raw)
        if parsed is not None:
            return parsed, name
    return None, None


def _parse(raw: object) -> datetime | None:
    """EXIF datetimes are 'YYYY:MM:DD HH:MM:SS'; blanks and zeroes are common."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip().rstrip("\x00")
    if not raw or raw.startswith("0000"):
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def date_suffix(when: datetime | None) -> str:
    """The bracketed tail of a caption: ' (Jun, 2023)', or ' (?, ?)' if unknown.

    Python builds this, never the model — that makes the format structurally
    impossible to get wrong rather than merely instructed.
    """
    if when is None:
        return UNKNOWN_SUFFIX
    return f" ({MONTHS[when.month - 1]}, {when.year})"
