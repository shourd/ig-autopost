"""Place a photo on a white 4:5 canvas with a minimum margin.

The rule: scale the photo (preserving aspect, never upscaling) to the largest
size that fits inside a (canvas_w - 2*margin) x (canvas_h - 2*margin) box, then
centre it. The limiting axis lands on exactly `min_margin_px`; the other axis
gets more.

Output is always a baseline sRGB JPEG at exactly canvas_w x canvas_h with no
EXIF and no ICC profile attached — Meta rejects PNG/HEIC, converts colour
anyway, and a stray Lightroom profile shifts colours on the way through.
"""

from __future__ import annotations

import argparse
import io
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

from src.config import BorderConfig, load_config

# Below this the photo would be visibly mushy; we stop stepping quality down
# and let the caller deal with a file that is still too big.
MIN_QUALITY = 60


@dataclass(frozen=True)
class BorderResult:
    src: Path
    dst: Path
    photo_w: int
    photo_h: int
    margin_left: int
    margin_top: int
    margin_right: int
    margin_bottom: int
    quality: int
    bytes: int
    upscale_blocked: bool

    def describe(self) -> str:
        flag = "  [not upscaled]" if self.upscale_blocked else ""
        return (
            f"{self.src.name:>18}  ->  {self.photo_w:>4} x {self.photo_h:<4}  "
            f"L/R {self.margin_left}/{self.margin_right}  "
            f"T/B {self.margin_top}/{self.margin_bottom}  "
            f"q{self.quality}  {self.bytes / 1024:.0f} KB{flag}"
        )


def _to_srgb(im: Image.Image) -> Image.Image:
    """Convert to sRGB RGB, honouring any embedded ICC profile.

    An image with no profile is assumed to already be sRGB. A profile we can't
    parse is better ignored than fatal — the photo still publishes, it just
    goes out untransformed.
    """
    icc = im.info.get("icc_profile")
    if icc:
        try:
            src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            return ImageCms.profileToProfile(
                im, src_profile, ImageCms.createProfile("sRGB"), outputMode="RGB"
            )
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass
    return im.convert("RGB")


def add_border(
    src: Path | str,
    dst: Path | str,
    cfg: BorderConfig | None = None,
) -> BorderResult:
    cfg = cfg or load_config().border
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(src) as raw:
        # Rotate BEFORE measuring, or a portrait shot from a rotated sensor
        # gets measured as landscape and comes out with the wrong margins.
        im = ImageOps.exif_transpose(raw)
        im = _to_srgb(im)

        src_w, src_h = im.size
        scale = min(cfg.fit_w / src_w, cfg.fit_h / src_h)

        # Never upscale. A photo smaller than the fit box keeps its own size
        # and simply gets fatter margins; the curation app badges it so a
        # too-small export doesn't quietly break the grid's house style.
        upscale_blocked = scale > 1.0
        if upscale_blocked:
            scale = 1.0

        photo_w = min(round(src_w * scale), cfg.fit_w)
        photo_h = min(round(src_h * scale), cfg.fit_h)
        photo = im.resize((photo_w, photo_h), Image.Resampling.LANCZOS)

    # A brand-new canvas is what strips EXIF and ICC: there is nothing in
    # .info to carry over, so save() writes neither.
    canvas = Image.new("RGB", (cfg.canvas_w, cfg.canvas_h), cfg.background)
    offset_x = (cfg.canvas_w - photo_w) // 2
    offset_y = (cfg.canvas_h - photo_h) // 2
    canvas.paste(photo, (offset_x, offset_y))

    # Meta's aspect floor is 4:5 exactly; 1080x1350 sits on it, so any drift
    # here fails container creation with an unhelpful error.
    assert canvas.size == (cfg.canvas_w, cfg.canvas_h), (
        f"canvas is {canvas.size}, must be {(cfg.canvas_w, cfg.canvas_h)}"
    )

    quality = cfg.jpeg_quality
    while True:
        canvas.save(
            dst,
            "JPEG",
            quality=quality,
            optimize=True,
            progressive=False,
            # 4:4:4 — chroma subsampling fringes exactly where this image has
            # its sharpest edge, the photo/border boundary.
            subsampling=0,
        )
        size = dst.stat().st_size
        if size <= cfg.max_bytes or quality <= MIN_QUALITY:
            break
        quality -= 4

    if size > cfg.max_bytes:
        raise ValueError(
            f"{dst.name} is {size / 1e6:.1f} MB at quality {quality}, over the "
            f"{cfg.max_bytes / 1e6:.0f} MB Meta limit"
        )

    return BorderResult(
        src=src,
        dst=dst,
        photo_w=photo_w,
        photo_h=photo_h,
        margin_left=offset_x,
        margin_top=offset_y,
        margin_right=cfg.canvas_w - photo_w - offset_x,
        margin_bottom=cfg.canvas_h - photo_h - offset_y,
        quality=quality,
        bytes=size,
        upscale_blocked=upscale_blocked,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", nargs="+", type=Path, help="source image(s)")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="output file (single src) or directory; defaults to photos/processed/",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    for src in args.src:
        if args.out and len(args.src) == 1 and args.out.suffix:
            dst = args.out
        else:
            out_dir = args.out or cfg.paths.processed
            dst = out_dir / f"{src.stem}.jpg"
        print(add_border(src, dst, cfg.border).describe())
    return 0


if __name__ == "__main__":
    sys.exit(main())
