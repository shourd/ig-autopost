"""Taking photos out of the queue, and putting it in date order."""

from __future__ import annotations

import dataclasses

import pytest

from app import state as state_module
from app.state import Photo, State
from src.config import load_config


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A State over a throwaway folder, never touching the real queue."""
    monkeypatch.setattr(state_module, "STATE_PATH", tmp_path / "state.json")

    cfg = load_config()
    paths = dataclasses.replace(
        cfg.paths,
        raw=tmp_path / "raw",
        processed=tmp_path / "processed",
        removed=tmp_path / "removed",
    )
    for folder in (paths.raw, paths.processed):
        folder.mkdir(parents=True)

    blank = State.__new__(State)  # __init__ would scan the real photos/raw
    blank.cfg = dataclasses.replace(cfg, paths=paths)
    blank.photos = {}
    blank.order = []
    return blank


def add(state, name, extra=(), date=None):
    photo = Photo(file=name, extra=list(extra), date=date)
    state.photos[name] = photo
    state.order.append(name)
    for member in photo.files:
        (state.cfg.paths.raw / member).write_bytes(b"raw")
        state.processed_path(member).write_bytes(b"rendered")
    return photo


# --- removing --------------------------------------------------------------


def test_removing_moves_the_raw_file_rather_than_deleting_it(state):
    add(state, "a.jpg")

    moved = state.remove("a.jpg")

    assert moved == ["a.jpg"]
    assert (state.cfg.paths.removed / "a.jpg").read_bytes() == b"raw"
    assert not (state.cfg.paths.raw / "a.jpg").exists()
    assert state.order == [] and state.photos == {}


def test_the_processed_render_goes_for_real(state):
    """It's derived — keeping it would leave a stale file to be published."""
    add(state, "a.jpg")

    state.remove("a.jpg")

    assert not state.processed_path("a.jpg").exists()
    assert not (state.cfg.paths.removed / "a.jpg").with_suffix(".processed").exists()


def test_a_carousel_leaves_as_one_post(state):
    add(state, "A.jpg", extra=["B.jpg", "C.jpg"])

    moved = state.remove("A.jpg")

    assert moved == ["A.jpg", "B.jpg", "C.jpg"]
    assert sorted(p.name for p in state.cfg.paths.removed.iterdir()) == [
        "A.jpg", "B.jpg", "C.jpg",
    ]


def test_removing_the_same_name_twice_keeps_both(state):
    """Photos get re-dropped and re-removed; the first one mustn't be clobbered."""
    add(state, "a.jpg")
    state.remove("a.jpg")
    (state.cfg.paths.raw / "a.jpg").write_bytes(b"second")
    state.photos["a.jpg"] = Photo(file="a.jpg")
    state.order.append("a.jpg")

    state.remove("a.jpg")

    kept = sorted(p.read_bytes() for p in state.cfg.paths.removed.iterdir())
    assert kept == [b"raw", b"second"]


def test_removing_an_unknown_photo_raises(state):
    with pytest.raises(KeyError):
        state.remove("nope.jpg")


def test_the_others_keep_their_order(state):
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        add(state, name)

    state.remove("b.jpg")

    assert state.order == ["a.jpg", "c.jpg"]


# --- ordering --------------------------------------------------------------


def test_sort_puts_the_oldest_first(state):
    add(state, "new.jpg", date="2026-08-07T10:00:00")
    add(state, "old.jpg", date="2016-05-06T09:00:00")
    add(state, "middle.jpg", date="2023-11-18T17:00:00")

    state.sort_by_date()

    assert state.order == ["old.jpg", "middle.jpg", "new.jpg"]


def test_undated_photos_go_to_the_back(state):
    """A photo with no EXIF date has no place in a chronology; don't invent one."""
    add(state, "dated.jpg", date="2024-01-01T00:00:00")
    add(state, "undated.jpg", date=None)

    state.sort_by_date()

    assert state.order == ["dated.jpg", "undated.jpg"]


def test_photos_taken_at_the_same_moment_sort_by_name(state):
    add(state, "b.jpg", date="2024-01-01T00:00:00")
    add(state, "a.jpg", date="2024-01-01T00:00:00")

    state.sort_by_date()

    assert state.order == ["a.jpg", "b.jpg"]
