"""Centre-crop a photo to a given aspect ratio, keeping the original.

    uv run python -m src.crop photos/raw/IMG_5666.jpg 2:3

The border renderer never crops — it fits the whole frame inside the canvas and
pads with white. That is the right default, but it means aspect ratio decides
how much white a photo gets: a 2:3 portrait lands with 107px of margin at the
sides, while a squarer 4:5 one nearly fills the canvas. On a grid where every
other vertical is 2:3, the squarer one reads as bigger and pushier.

Cropping to match is the fix, and it belongs here rather than inside
`add_border`: throwing away pixels is a decision about a specific photograph,
not a rendering rule, and it should be visible in the shell history rather than
happening quietly to everything.

The original is moved to `photos/removed/` first, so this is reversible.
"""

from __future__ import annotations

import argparse
import sys
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageOps

from src.config import load_config


def parse_ratio(text: str) -> float:
    """"2:3", "0.667" and "2/3" all mean the same thing."""
    if ":" in text:
        w, h = text.split(":", 1)
        return float(w) / float(h)
    return float(Fraction(text))


def crop_to(path: Path, ratio: float, backup_dir: Path | None = None) -> tuple[int, int]:
    """Centre-crop `path` in place to `ratio` (width / height). Returns the new size.

    Rotation is applied first — a portrait photo tagged sideways would otherwise
    be measured as landscape and cropped along the wrong axis.
    """
    image = ImageOps.exif_transpose(Image.open(path))
    width, height = image.size
    current = width / height

    if abs(current - ratio) < 0.005:
        return width, height

    if current > ratio:  # too wide: take it in from the sides
        new_w, new_h = round(height * ratio), height
    else:  # too tall: take it off the top and bottom
        new_w, new_h = width, round(width / ratio)

    left, top = (width - new_w) // 2, (height - new_h) // 2
    cropped = image.crop((left, top, left + new_w, top + new_h))

    if backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{path.stem} (uncropped){path.suffix}"
        image.save(backup, quality=95, subsampling=0)

    # Written without EXIF: the crop no longer matches the orientation tag that
    # was just applied, and add_border strips metadata anyway.
    cropped.save(path, quality=95, subsampling=0)
    return cropped.size


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("photo", type=Path)
    parser.add_argument("ratio", help='target width:height, e.g. "2:3"')
    parser.add_argument(
        "--no-backup", action="store_true", help="don't keep the original"
    )
    args = parser.parse_args()

    if not args.photo.is_file():
        print(f"  {args.photo} doesn't exist.", file=sys.stderr)
        raise SystemExit(1)

    cfg = load_config()
    before = ImageOps.exif_transpose(Image.open(args.photo)).size
    after = crop_to(
        args.photo,
        parse_ratio(args.ratio),
        backup_dir=None if args.no_backup else cfg.paths.removed,
    )
    print(f"  {args.photo.name}: {before[0]}x{before[1]} -> {after[0]}x{after[1]}")
    if before != after and not args.no_backup:
        print(f"  original kept in {cfg.paths.removed.name}/")


if __name__ == "__main__":
    main()
