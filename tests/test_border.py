"""Phase 1 spec tests.

Every geometry assertion measures the *saved output file*, not an intermediate
variable, so the test would catch a bug anywhere between the resize and the
final JPEG on disk.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageCms

from src.border import add_border
from tests.conftest import photo_bbox

# (name, source size, expected photo size, expected left/top margin)
CASES = [
    ("landscape_3x2", (3000, 2000), (1028, 685), (26, 332)),
    ("portrait_2x3", (2000, 3000), (865, 1298), (107, 26)),
    ("square_1x1", (2000, 2000), (1028, 1028), (26, 161)),
]


@pytest.fixture(scope="session")
def outputs(make_photo, tmp_path_factory, cfg):
    """Run all three spec cases once; tests below inspect the results."""
    out_dir = tmp_path_factory.mktemp("processed")
    results = {}
    for name, (w, h), _, _ in CASES:
        src = make_photo(f"{name}.jpg", w, h)
        results[name] = add_border(src, out_dir / f"{name}.jpg", cfg)
    return results


# --- the three expected outputs from the spec -----------------------------


@pytest.mark.parametrize("name,src_size,photo,margin", CASES, ids=[c[0] for c in CASES])
def test_photo_geometry(outputs, name, src_size, photo, margin):
    r = outputs[name]
    assert (r.photo_w, r.photo_h) == photo
    assert (r.margin_left, r.margin_top) == margin


@pytest.mark.parametrize("name,src_size,photo,margin", CASES, ids=[c[0] for c in CASES])
def test_photo_geometry_measured_on_disk(outputs, name, src_size, photo, margin):
    """The same numbers, read back off the rendered pixels."""
    r = outputs[name]
    left, top = margin
    assert photo_bbox(r.dst) == (left, top, left + photo[0], top + photo[1])


@pytest.mark.parametrize("name,src_size,photo,margin", CASES, ids=[c[0] for c in CASES])
def test_limiting_axis_gets_exactly_min_margin(outputs, cfg, name, src_size, photo, margin):
    """Whichever axis is tight must land on min_margin_px exactly."""
    r = outputs[name]
    assert cfg.min_margin_px in (r.margin_left, r.margin_top)
    assert min(r.margin_left, r.margin_top) == cfg.min_margin_px


def test_margins_are_symmetric_or_off_by_one(outputs):
    """1350-685 and 1080-865 are odd, so one side carries the extra pixel."""
    for r in outputs.values():
        assert abs(r.margin_left - r.margin_right) <= 1
        assert abs(r.margin_top - r.margin_bottom) <= 1
        assert r.margin_left + r.photo_w + r.margin_right == 1080
        assert r.margin_top + r.photo_h + r.margin_bottom == 1350


# --- Meta API constraints --------------------------------------------------


@pytest.mark.parametrize("name", [c[0] for c in CASES])
def test_canvas_is_exactly_1080x1350(outputs, name):
    """1080x1350 is exactly the 4:5 aspect floor; drift fails container creation."""
    with Image.open(outputs[name].dst) as im:
        assert im.size == (1080, 1350)
        assert im.size[0] / im.size[1] == 0.8


@pytest.mark.parametrize("name", [c[0] for c in CASES])
def test_output_is_jpeg_rgb_under_8mb(outputs, cfg, name):
    r = outputs[name]
    with Image.open(r.dst) as im:
        assert im.format == "JPEG"
        assert im.mode == "RGB"
    assert r.bytes == r.dst.stat().st_size
    assert r.bytes <= cfg.max_bytes


@pytest.mark.parametrize("name", [c[0] for c in CASES])
def test_no_exif_and_no_icc_in_output(outputs, name):
    with Image.open(outputs[name].dst) as im:
        assert "icc_profile" not in im.info
        assert not im.getexif()


def test_config_matches_meta_requirements(cfg):
    """Guard the config file itself — these values are not free parameters."""
    assert (cfg.canvas_w, cfg.canvas_h) == (1080, 1350)
    assert 320 <= cfg.canvas_w <= 1440
    assert 0.8 <= cfg.canvas_w / cfg.canvas_h <= 1.91
    assert cfg.min_margin_px == 26
    assert cfg.jpeg_quality == 92
    assert cfg.max_bytes == 8 * 1024 * 1024


# --- EXIF orientation ------------------------------------------------------


def test_exif_rotation_applied_before_measuring(make_photo, tmp_path, cfg):
    """A 3000x2000 file tagged orientation=6 displays as portrait 2000x3000.

    If we measured before transposing we'd get the landscape result (1028x685);
    the portrait result proves the rotation happened first.
    """
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 CW to display
    src = make_photo("rotated.jpg", 3000, 2000, exif=exif)

    r = add_border(src, tmp_path / "rotated.jpg", cfg)

    assert (r.photo_w, r.photo_h) == (865, 1298)
    assert (r.margin_left, r.margin_top) == (107, 26)


# --- never upscale ---------------------------------------------------------


def test_small_photo_is_not_upscaled(make_photo, tmp_path, cfg):
    src = make_photo("tiny.jpg", 400, 300)

    r = add_border(src, tmp_path / "tiny.jpg", cfg)

    assert (r.photo_w, r.photo_h) == (400, 300)
    assert r.upscale_blocked is True
    # Still centred on the full canvas, just with fatter margins than 26.
    assert (r.margin_left, r.margin_top) == (340, 525)
    assert photo_bbox(r.dst) == (340, 525, 740, 825)


@pytest.mark.parametrize("name", [c[0] for c in CASES])
def test_large_photos_do_not_set_upscale_flag(outputs, name):
    assert outputs[name].upscale_blocked is False


def test_photo_exactly_fit_box_is_not_flagged(make_photo, tmp_path, cfg):
    """Boundary: scale == 1.0 exactly is a fit, not a blocked upscale."""
    src = make_photo("exact.jpg", cfg.fit_w, cfg.fit_h)

    r = add_border(src, tmp_path / "exact.jpg", cfg)

    assert (r.photo_w, r.photo_h) == (1028, 1298)
    assert r.upscale_blocked is False
    assert (r.margin_left, r.margin_top) == (26, 26)


# --- colour management -----------------------------------------------------


def test_embedded_icc_profile_is_stripped(make_photo, tmp_path, cfg):
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
    src = make_photo("profiled.jpg", 3000, 2000, icc_profile=profile.tobytes())
    with Image.open(src) as im:
        assert im.info.get("icc_profile"), "fixture should carry a profile"

    r = add_border(src, tmp_path / "profiled.jpg", cfg)

    with Image.open(r.dst) as im:
        assert "icc_profile" not in im.info
    assert (r.photo_w, r.photo_h) == (1028, 685)


def test_unparseable_icc_profile_does_not_crash(make_photo, tmp_path, cfg):
    """A corrupt profile should cost colour accuracy, not the whole run."""
    src = make_photo("badicc.jpg", 3000, 2000, icc_profile=b"not a real profile")

    r = add_border(src, tmp_path / "badicc.jpg", cfg)

    assert (r.photo_w, r.photo_h) == (1028, 685)
    with Image.open(r.dst) as im:
        assert im.mode == "RGB"
        assert "icc_profile" not in im.info


def test_grayscale_source_becomes_rgb(make_photo, tmp_path, cfg):
    src = make_photo("gray_src.jpg", 3000, 2000)
    gray = tmp_path / "gray.jpg"
    with Image.open(src) as im:
        im.convert("L").save(gray, "JPEG", quality=95)

    r = add_border(gray, tmp_path / "gray_out.jpg", cfg)

    with Image.open(r.dst) as im:
        assert im.mode == "RGB"
    assert (r.photo_w, r.photo_h) == (1028, 685)


# --- background ------------------------------------------------------------


def test_background_is_pure_white_away_from_the_photo(outputs):
    with Image.open(outputs["landscape_3x2"].dst) as im:
        for xy in [(0, 0), (1079, 0), (0, 1349), (1079, 1349), (540, 4)]:
            assert im.getpixel(xy) == (255, 255, 255), f"{xy} is not white"
