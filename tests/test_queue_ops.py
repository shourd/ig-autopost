"""Taking photos out of the queue, and choosing what goes where in it."""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime

import pytest
import yaml

from app import state as state_module
from app.state import STATUS_POSTED, Photo, State
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
        posted=tmp_path / "posted",
        history=tmp_path / "history",
        queue=tmp_path / "queue.yaml",
    )
    for folder in (paths.raw, paths.processed, paths.posted, paths.history):
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


# --- shuffling -------------------------------------------------------------


@pytest.fixture
def reversing_shuffle(monkeypatch):
    """A deterministic stand-in, so the assertions can be exact."""
    monkeypatch.setattr(state_module.random, "shuffle", lambda seq: seq.reverse())


def test_shuffle_deals_the_queue_out_again(state, reversing_shuffle):
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        add(state, name)

    state.shuffle()

    assert state.order == ["c.jpg", "b.jpg", "a.jpg"]


def test_posted_photos_keep_their_place(state, reversing_shuffle):
    """History is the record of what went out; shuffling it would be a lie."""
    add(state, "a.jpg")
    add(state, "gone.jpg").status = STATUS_POSTED
    add(state, "b.jpg")
    add(state, "c.jpg")

    state.shuffle()

    assert state.order == ["c.jpg", "gone.jpg", "b.jpg", "a.jpg"]


def test_shuffle_loses_nothing(state):
    names = [f"{i}.jpg" for i in range(20)]
    for name in names:
        add(state, name)

    state.shuffle()

    assert sorted(state.order) == sorted(names)


# --- choosing which photo of a carousel leads ------------------------------


def carousel(state, base="_DSF1234", letters="ABC"):
    """A carousel whose files can be told apart by their contents."""
    photo = add(state, f"{base}A.jpg", extra=[f"{base}{x}.jpg" for x in letters[1:]])
    for member in photo.files:
        (state.cfg.paths.raw / member).write_bytes(member.encode())
        state.processed_path(member).write_bytes(f"render {member}".encode())
    return photo


def content(state, name):
    return (state.cfg.paths.raw / name).read_bytes().decode()


def test_promoting_the_second_photo_swaps_the_letters(state):
    carousel(state)

    renamed = state.promote("_DSF1234A.jpg", "_DSF1234B.jpg")

    assert sorted(renamed) == [
        ("_DSF1234A.jpg", "_DSF1234B.jpg"),
        ("_DSF1234B.jpg", "_DSF1234A.jpg"),
    ]
    assert content(state, "_DSF1234A.jpg") == "_DSF1234B.jpg"
    assert content(state, "_DSF1234B.jpg") == "_DSF1234A.jpg"


def test_the_photos_left_behind_keep_their_order(state):
    """Promoting C means C, A, B — not C and then whatever."""
    carousel(state)

    state.promote("_DSF1234A.jpg", "_DSF1234C.jpg")

    assert [content(state, f"_DSF1234{x}.jpg") for x in "ABC"] == [
        "_DSF1234C.jpg", "_DSF1234A.jpg", "_DSF1234B.jpg",
    ]


def test_the_renders_follow_their_photos(state):
    """Otherwise the grid would show the old lead until something re-rendered."""
    carousel(state)

    state.promote("_DSF1234A.jpg", "_DSF1234B.jpg")

    assert state.processed_path("_DSF1234A.jpg").read_bytes() == b"render _DSF1234B.jpg"


def test_the_post_keeps_its_caption_and_its_place_in_the_queue(state):
    photo = carousel(state)
    photo.caption = "Written already"

    state.promote("_DSF1234A.jpg", "_DSF1234B.jpg")

    assert state.order == ["_DSF1234A.jpg"]
    assert state.photos["_DSF1234A.jpg"] is photo
    assert photo.files == ["_DSF1234A.jpg", "_DSF1234B.jpg", "_DSF1234C.jpg"]


def test_promoting_the_photo_that_already_leads_does_nothing(state):
    carousel(state)

    assert state.promote("_DSF1234A.jpg", "_DSF1234A.jpg") == []
    assert content(state, "_DSF1234A.jpg") == "_DSF1234A.jpg"


def test_promoting_a_photo_from_another_post_raises(state):
    carousel(state)
    add(state, "other.jpg")

    with pytest.raises(KeyError):
        state.promote("_DSF1234A.jpg", "other.jpg")


def test_no_stray_temporaries_are_left_behind(state):
    carousel(state)

    state.promote("_DSF1234A.jpg", "_DSF1234C.jpg")

    for folder in (state.cfg.paths.raw, state.cfg.paths.processed):
        assert not [p.name for p in folder.iterdir() if p.name.startswith(".")]


# --- the already-posted block ----------------------------------------------


def publish(state, name, when, files=None, recorded=True):
    """A post that has already gone out, with its file and its queue entry.

    `recorded=False` is the backfill from the API: a file with no entry in
    queue.yaml, carrying its post time on the mtime.
    """
    stamp = datetime.fromisoformat(when).timestamp()
    folder = state.cfg.paths.posted if recorded else state.cfg.paths.history
    for member in files or [name]:
        path = folder / member
        path.write_bytes(b"posted")
        # Deliberately wrong on the file: this is the render time a move
        # preserves, which is what the grid used to sort on.
        os.utime(path, (stamp if not recorded else 0, stamp if not recorded else 0))
    if recorded:
        entry = {"file": name, "status": "posted", "posted_at": when}
        if files:
            entry["files"] = files
        existing = yaml.safe_load(state.cfg.paths.queue.read_text()) if state.cfg.paths.queue.exists() else []
        state.cfg.paths.queue.write_text(yaml.safe_dump((existing or []) + [entry], sort_keys=False))


def test_the_grid_follows_the_real_publish_times(state):
    """Not the file mtimes: moving a rendered file preserves its render time,
    and a batch rendered in one sitting says nothing about publishing order."""
    publish(state, "second.jpg", "2026-08-20T10:00:00+00:00")
    publish(state, "newest.jpg", "2026-09-01T10:00:00+00:00")
    publish(state, "oldest.jpg", "2026-08-15T10:00:00+00:00")

    assert state.posted() == ["newest.jpg", "second.jpg", "oldest.jpg"]


def test_the_backfill_still_sorts_on_its_mtime(state):
    """Photos downloaded from the API have no queue entry; src.history stamps
    the real post time onto the file instead."""
    publish(state, "mine.jpg", "2026-08-20T10:00:00+00:00")
    publish(state, "2024-01-28_abc.jpg", "2024-01-28T10:00:00+00:00", recorded=False)
    publish(state, "2026-08-25_xyz.jpg", "2026-08-25T10:00:00+00:00", recorded=False)

    assert state.posted() == ["2026-08-25_xyz.jpg", "mine.jpg", "2024-01-28_abc.jpg"]


def test_a_published_carousel_takes_one_square(state):
    publish(state, "A_A.jpg", "2026-09-01T10:00:00+00:00", files=["A_A.jpg", "A_B.jpg"])
    publish(state, "solo.jpg", "2026-08-01T10:00:00+00:00")

    assert state.posted() == ["A_A.jpg", "solo.jpg"]


# --- the app pushing an order back -----------------------------------------


def test_a_reorder_only_moves_what_the_app_sent(state):
    """The app draws the queue, not the history, so its order names only the
    unpublished posts — and the published ones must not drift because of it."""
    add(state, "a.jpg")
    add(state, "posted.jpg").status = STATUS_POSTED
    add(state, "b.jpg")
    add(state, "c.jpg")

    state.reorder(["c.jpg", "b.jpg", "a.jpg"])

    assert state.order == ["c.jpg", "posted.jpg", "b.jpg", "a.jpg"]


def test_a_photo_missing_from_the_order_still_lands(state):
    """`photos` and `order` can drift apart for a moment; nothing may be lost."""
    add(state, "a.jpg")
    add(state, "b.jpg")
    state.order.remove("b.jpg")

    state.reorder(["b.jpg", "a.jpg"])

    assert state.order == ["b.jpg", "a.jpg"]


def test_names_the_state_does_not_know_are_ignored(state):
    add(state, "a.jpg")
    add(state, "b.jpg")

    state.reorder(["b.jpg", "ghost.jpg", "a.jpg"])

    assert state.order == ["b.jpg", "a.jpg"]
