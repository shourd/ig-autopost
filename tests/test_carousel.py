"""Carousels: grouped by filename, published as one post."""

from __future__ import annotations

import pytest

from app.state import Photo, carousel_groups
from src.config import load_config
from src.publish import create_container

CFG = load_config()


# --- grouping by filename --------------------------------------------------


def test_letter_suffix_groups_into_one_post():
    groups = carousel_groups(["_DSF1234A.jpg", "_DSF1234B.jpg", "_DSF1234C.jpg"])

    assert groups == {"_DSF1234A.jpg": ["_DSF1234B.jpg", "_DSF1234C.jpg"]}


def test_a_lone_lettered_file_is_still_a_single_post():
    """Otherwise any filename ending in a letter would become a carousel of one."""
    assert carousel_groups(["sunsetA.jpg", "_DSF9999.jpg"]) == {}


def test_unrelated_files_do_not_group():
    groups = carousel_groups(["_DSF0001.jpg", "_DSF0002.jpg", "beachB.jpg"])

    assert groups == {}


def test_lowercase_and_late_letters_are_not_suffixes():
    """A/B/C…J only — 'photo-final.jpg' and 'shotZ.jpg' aren't sequences."""
    assert carousel_groups(["shota.jpg", "shotb.jpg"]) == {}
    assert carousel_groups(["shotY.jpg", "shotZ.jpg"]) == {}


def test_more_than_ten_photos_are_truncated_to_metas_limit():
    names = [f"trip{c}.jpg" for c in "ABCDEFGHIJ"]
    extra = carousel_groups([*names, "tripK.jpg"])["tripA.jpg"]

    assert len(extra) == 9  # lead + 9 = 10


def test_queue_entry_lists_files_only_for_a_carousel():
    single = Photo(file="a.jpg")
    carousel = Photo(file="_DSF1A.jpg", extra=["_DSF1B.jpg"])

    assert "files" not in single.queue_entry()
    assert carousel.queue_entry()["files"] == ["_DSF1A.jpg", "_DSF1B.jpg"]
    # `file` stays the lead, so anything reading one image still finds one.
    assert carousel.queue_entry()["file"] == "_DSF1A.jpg"


# --- the Meta call shape ---------------------------------------------------


@pytest.fixture
def calls(monkeypatch):
    """Record container requests instead of making them."""
    recorded = []

    def fake_post(url, **data):
        recorded.append({"url": url, **data})
        return {"id": f"container{len(recorded)}"}

    monkeypatch.setattr("src.publish._post", fake_post)
    monkeypatch.setattr("src.publish.await_ready", lambda *a, **k: None)
    return recorded


def test_single_photo_is_one_container(calls):
    photo = Photo(file="a.jpg", caption="a leopard (Jun, 2023)")

    create_container(photo, ["https://x/a.jpg"], "tok", "ig", CFG)

    assert len(calls) == 1
    assert calls[0]["image_url"] == "https://x/a.jpg"
    assert calls[0]["caption"] == "a leopard (Jun, 2023)"
    assert "is_carousel_item" not in calls[0]


def test_carousel_creates_children_then_a_parent(calls):
    photo = Photo(file="A.jpg", extra=["B.jpg"], caption="the pair (Jun, 2023)")

    container = create_container(photo, ["https://x/A.jpg", "https://x/B.jpg"], "tok", "ig", CFG)

    children, parent = calls[:2], calls[2]
    assert [c["image_url"] for c in children] == ["https://x/A.jpg", "https://x/B.jpg"]
    assert all(c["is_carousel_item"] == "true" for c in children)
    # A caption on a child is silently dropped, so it must only be on the parent.
    assert all("caption" not in c for c in children)
    assert parent["media_type"] == "CAROUSEL"
    assert parent["children"] == "container1,container2"
    assert parent["caption"] == "the pair (Jun, 2023)"
    assert container == "container3"


def test_location_id_rides_on_the_parent_only(calls):
    photo = Photo(file="A.jpg", extra=["B.jpg"], location_id="123")

    create_container(photo, ["https://x/A.jpg", "https://x/B.jpg"], "tok", "ig", CFG)

    assert all("location_id" not in c for c in calls[:2])
    assert calls[2]["location_id"] == "123"
