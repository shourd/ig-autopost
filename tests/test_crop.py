"""Centre-cropping a photo so it wears the same margins as the rest."""

from __future__ import annotations

import pytest
from PIL import Image

from src.crop import crop_to, parse_ratio


@pytest.mark.parametrize("text,expected", [("2:3", 2 / 3), ("4:5", 0.8), ("0.75", 0.75), ("3/2", 1.5)])
def test_ratio_notation(text, expected):
    assert parse_ratio(text) == pytest.approx(expected)


def make(tmp_path, name, w, h, exif=None):
    path = tmp_path / name
    Image.new("RGB", (w, h), "grey").save(path, exif=exif or Image.Exif())
    return path


def test_a_squarer_portrait_is_taken_in_from_the_sides(tmp_path):
    photo = make(tmp_path, "wide.jpg", 2430, 3024)  # 0.804, the odd one out

    size = crop_to(photo, 2 / 3, backup_dir=tmp_path / "kept")

    assert size == (2016, 3024)  # height untouched
    assert Image.open(photo).size == (2016, 3024)


def test_a_taller_photo_is_taken_off_the_top_and_bottom(tmp_path):
    photo = make(tmp_path, "tall.jpg", 1000, 2000)

    assert crop_to(photo, 2 / 3, backup_dir=None) == (1000, 1500)


def test_the_crop_is_centred(tmp_path):
    """Both edges give up the same amount, so the composition holds."""
    image = Image.new("RGB", (300, 300), "white")
    for x in range(100, 200):
        for y in range(300):
            image.putpixel((x, y), (0, 0, 0))  # a black stripe down the middle
    photo = tmp_path / "stripe.jpg"
    image.save(photo)

    crop_to(photo, 2 / 3, backup_dir=None)

    cropped = Image.open(photo)
    assert cropped.size == (200, 300)
    # The stripe was centred before and must still be: 100px wide, 50px in.
    assert cropped.getpixel((49, 150))[0] > 200
    assert cropped.getpixel((100, 150))[0] < 60
    assert cropped.getpixel((151, 150))[0] > 200


def test_a_photo_already_at_the_ratio_is_left_alone(tmp_path):
    photo = make(tmp_path, "fine.jpg", 2000, 3000)
    before = photo.read_bytes()

    assert crop_to(photo, 2 / 3, backup_dir=tmp_path / "kept") == (2000, 3000)
    assert photo.read_bytes() == before  # not even re-encoded
    assert not (tmp_path / "kept").exists()


def test_the_original_is_kept(tmp_path):
    photo = make(tmp_path, "wide.jpg", 2430, 3024)

    crop_to(photo, 2 / 3, backup_dir=tmp_path / "kept")

    backup = tmp_path / "kept" / "wide (uncropped).jpg"
    assert Image.open(backup).size == (2430, 3024)


def test_orientation_is_applied_before_measuring(tmp_path):
    """A sideways-tagged portrait would otherwise be cropped along the wrong axis."""
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW: stored 3024x2430, displayed 2430x3024
    photo = make(tmp_path, "rotated.jpg", 3024, 2430, exif=exif)

    assert crop_to(photo, 2 / 3, backup_dir=None) == (2016, 3024)
