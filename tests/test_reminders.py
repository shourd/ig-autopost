"""Todoist reminders: one task per upcoming post, rewritten on every Save."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from app.state import STATUS_HOLD, STATUS_READY, Photo
from src import reminders
from src.config import load_config


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeTodoist:
    """Records calls; hands out task ids in order."""

    def __init__(self, fail=False):
        self.posts, self.deletes = [], []
        self.fail = fail

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "body": json})
        if self.fail:
            return FakeResponse(500)
        return FakeResponse(payload={"id": f"task{len(self.posts)}"})

    def delete(self, url, headers=None, timeout=None):
        self.deletes.append(url)
        return FakeResponse(204)


class FakeState:
    def __init__(self, photos, count=4):
        cfg = load_config()
        self.cfg = dataclasses.replace(
            cfg, publish=dataclasses.replace(cfg.publish, reminders=True, reminder_count=count)
        )
        self.photos = {p.file: p for p in photos}

    def ordered(self):
        return list(self.photos.values())

    def upcoming(self, count):
        start = datetime(2026, 8, 19, 9, 52, tzinfo=timezone.utc)
        return [start + timedelta(days=2 * n) for n in range(count)]

    def processed_path(self, name):
        return Path("photos/processed") / name


def url_for(name, cfg):
    return f"https://raw.githubusercontent.com/o/r/main/photos/processed/{name}"


@pytest.fixture
def todoist(monkeypatch):
    fake = FakeTodoist()
    monkeypatch.setattr(reminders, "requests", fake)
    monkeypatch.setattr(reminders, "secret", lambda *a, **k: "token")
    return fake


def test_creates_one_task_per_upcoming_post(todoist):
    state = FakeState([Photo(file="a.jpg", caption="one"), Photo(file="b.jpg", caption="two")])

    reminders.sync(state, url_for)

    assert len(todoist.posts) == 2
    assert [p.reminder_id for p in state.photos.values()] == ["task1", "task2"]
    body = todoist.posts[0]["body"]
    assert body["content"] == "Post a.jpg to Instagram"
    assert body["due_datetime"] == "2026-08-19T09:52:00+00:00"
    assert body["auto_reminder"] is True
    assert "one" in body["description"]


def test_the_task_carries_every_photo_of_a_carousel(todoist):
    state = FakeState([Photo(file="A.jpg", extra=["B.jpg"], caption="the pair")])

    reminders.sync(state, url_for)

    description = todoist.posts[0]["body"]["description"]
    assert "photos/processed/A.jpg" in description
    assert "photos/processed/B.jpg" in description
    assert "Carousel of 2" in description


def test_a_missing_caption_is_called_out(todoist):
    state = FakeState([Photo(file="a.jpg")])

    reminders.sync(state, url_for)

    assert "No caption written yet" in todoist.posts[0]["body"]["description"]


def test_an_existing_task_is_updated_not_duplicated(todoist):
    photo = Photo(file="a.jpg", caption="one", reminder_id="task99")
    state = FakeState([photo])

    reminders.sync(state, url_for)

    assert todoist.posts[0]["url"].endswith("/tasks/task99")
    assert photo.reminder_id == "task99"


def test_reordering_past_the_window_clears_the_task(todoist):
    """The schedule moves when the queue does; a stale reminder is worse than none."""
    kept = Photo(file="a.jpg", caption="one", reminder_id="task1")
    dropped = Photo(file="b.jpg", caption="two", reminder_id="task2")
    state = FakeState([kept, dropped], count=1)

    reminders.sync(state, url_for)

    assert todoist.deletes == [f"{reminders.API}/task2"]
    assert dropped.reminder_id is None
    assert kept.reminder_id == "task1"


def test_held_and_posted_photos_get_no_reminder(todoist):
    state = FakeState([
        Photo(file="a.jpg", status=STATUS_HOLD),
        Photo(file="b.jpg", status=STATUS_READY, caption="two"),
    ])

    reminders.sync(state, url_for)

    assert [p["body"]["content"] for p in todoist.posts] == ["Post b.jpg to Instagram"]


def test_a_todoist_outage_is_reported_never_raised(monkeypatch):
    monkeypatch.setattr(reminders, "requests", FakeTodoist(fail=True))
    monkeypatch.setattr(reminders, "secret", lambda *a, **k: "token")
    state = FakeState([Photo(file="a.jpg", caption="one")])

    log = reminders.sync(state, url_for)

    assert any("failed" in line for line in log)
    assert state.photos["a.jpg"].reminder_id is None


def test_no_token_is_a_note_not_a_failure(monkeypatch):
    monkeypatch.setattr(reminders, "secret", lambda *a, **k: None)
    state = FakeState([Photo(file="a.jpg")])

    assert reminders.sync(state, url_for) == [
        "reminders skipped (no TODOIST_API_TOKEN in .env)"
    ]
