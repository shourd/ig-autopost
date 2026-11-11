"""Todoist reminders: one task per upcoming post, rewritten on every Save."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
import types
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

    def __init__(self, fail=False, alarm_status=200, existing_alarms=None):
        self.posts, self.deletes, self.alarms = [], [], []
        self.fail = fail
        self.alarm_status = alarm_status
        self.existing_alarms = existing_alarms or []

    def post(self, url, headers=None, json=None, timeout=None):
        if url == reminders.ALARMS:
            self.alarms.append(json)
            return FakeResponse(self.alarm_status)
        self.posts.append({"url": url, "body": json})
        if self.fail:
            return FakeResponse(500)
        return FakeResponse(payload={"id": f"task{len(self.posts)}"})

    def get(self, url, headers=None, params=None, timeout=None):
        return FakeResponse(payload={"results": self.existing_alarms})

    def delete(self, url, headers=None, timeout=None):
        self.deletes.append(url)
        return FakeResponse(204)


class FakeState:
    def __init__(self, photos, count=4, lead=120, apple=False):
        cfg = load_config()
        self.cfg = dataclasses.replace(
            cfg,
            publish=dataclasses.replace(
                cfg.publish,
                reminders=True,
                reminder_count=count,
                reminder_lead_minutes=lead,
                # Off unless a test asks: the real thing talks to the Reminders
                # app, which no test should be poking at.
                reminder_apple=apple,
                reminder_apple_list=None,
            ),
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


@pytest.fixture(autouse=True)
def reminders_app(monkeypatch):
    """Stand in for the Reminders app, and record what it was asked to write.

    Needed even when the Apple channel is off: switched off, `_apple` sweeps
    with an empty list rather than returning early, so every path through
    `sync` reaches osascript unless something like this is in the way.
    """
    calls = []

    def fake_sync(nudges, list_name=None, run=None):
        calls.append(list(nudges))
        return [f"Apple Reminders: {len(nudges)} in the default list"]

    monkeypatch.setattr("src.apple_reminders.sync", fake_sync)
    return calls


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
    assert "one" in body["description"]
    # The push comes 2h early, so the description says when to actually post —
    # in local time (09:52 UTC is 11:52 in Amsterdam in August).
    assert "Post at Wed 19 Aug, 11:52." in body["description"]


def test_a_push_reminder_is_set_two_hours_ahead(todoist):
    state = FakeState([Photo(file="a.jpg", caption="one")])

    reminders.sync(state, url_for)

    assert todoist.alarms == [{
        "task_id": "task1",
        "reminder_type": "relative",
        "minute_offset": 120,
        "service": "push",
    }]
    # Todoist's own due-time reminder would double the notification.
    assert todoist.posts[0]["body"]["auto_reminder"] is False


def test_an_existing_alarm_is_not_duplicated(monkeypatch):
    fake = FakeTodoist(existing_alarms=[{"minute_offset": 120}])
    monkeypatch.setattr(reminders, "requests", fake)
    monkeypatch.setattr(reminders, "secret", lambda *a, **k: "token")
    photo = Photo(file="a.jpg", caption="one", reminder_id="task9")

    reminders.sync(FakeState([photo]), url_for)

    assert fake.alarms == []


def test_a_free_plan_is_explained_once(monkeypatch):
    """Reminders are a paid feature; the tasks still land, the push doesn't."""
    fake = FakeTodoist(alarm_status=403)
    monkeypatch.setattr(reminders, "requests", fake)
    monkeypatch.setattr(reminders, "secret", lambda *a, **k: "token")
    state = FakeState([Photo(file="a.jpg"), Photo(file="b.jpg")])

    log = reminders.sync(state, url_for)

    plan = [line for line in log if "paid Todoist plan" in line]
    assert len(plan) == 1
    assert len(todoist_ids(state)) == 2  # both tasks were still created


def todoist_ids(state):
    return [p.reminder_id for p in state.photos.values() if p.reminder_id]


def test_the_apple_reminder_fires_the_lead_time_before_the_slot(todoist, monkeypatch):
    captured = {}

    def fake_apple(nudges, list_name):
        captured["nudges"] = nudges
        captured["list"] = list_name
        return ["Apple Reminders: 1 in the default list"]

    monkeypatch.setattr("src.apple_reminders.sync", fake_apple)
    state = FakeState([Photo(file="a.jpg", caption="one")], apple=True)

    log = reminders.sync(state, url_for)

    nudge = captured["nudges"][0]
    assert nudge.when == datetime(2026, 8, 19, 7, 52, tzinfo=timezone.utc)  # 2h before
    assert nudge.title == "Post a.jpg to Instagram"
    assert "Post at Wed 19 Aug, 11:52." in nudge.body
    assert any("Apple Reminders" in line for line in log)
    # Todoist's premium-only alarm isn't worth attempting, or warning about,
    # once something else is doing the notifying.
    assert todoist.alarms == []
    assert not any("paid Todoist plan" in line for line in log)


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
        "Todoist skipped (no TODOIST_API_TOKEN in .env)"
    ]


# --- stray Todoist tasks ---------------------------------------------------


class FakeTasksApi:
    """Just enough of the tasks API to watch what gets deleted."""

    def __init__(self, tasks):
        self.tasks = tasks
        self.deleted = []

    def get(self, url, headers=None, params=None, timeout=None):
        return types.SimpleNamespace(
            status_code=200, raise_for_status=lambda: None,
            json=lambda: {"results": self.tasks, "next_cursor": None},
        )

    def delete(self, url, headers=None, timeout=None):
        self.deleted.append(url.rsplit("/", 1)[1])
        return types.SimpleNamespace(status_code=204, raise_for_status=lambda: None)


def test_a_task_the_state_lost_track_of_is_deleted(monkeypatch):
    """An id that was never saved can't be updated or cleared by id, so the
    next run would make a second task for the same photo."""
    fake = FakeTasksApi([
        {"id": "111", "content": "Post a.jpg to Instagram"},   # just written
        {"id": "222", "content": "Post b.jpg to Instagram"},   # orphan
    ])
    monkeypatch.setattr(reminders, "requests", fake)

    gone, problem = reminders._sweep("token", written={"111"})

    assert (gone, problem) == (1, None)
    assert fake.deleted == ["222"]


def test_the_sweep_leaves_everything_else_alone(monkeypatch):
    """It shares a Todoist account with the rest of Sjoerd's life."""
    fake = FakeTasksApi([
        {"id": "1", "content": "Buy milk"},
        {"id": "2", "content": "Post the letter"},            # prefix only
        {"id": "3", "content": "Reply to Instagram"},         # suffix only
    ])
    monkeypatch.setattr(reminders, "requests", fake)

    gone, _ = reminders._sweep("token", written=set())

    assert (gone, fake.deleted) == (0, [])


def test_a_todoist_outage_during_the_sweep_is_reported_not_raised(monkeypatch):
    class Dead(FakeTasksApi):
        def get(self, *a, **k):
            raise reminders.RequestException("down")

    monkeypatch.setattr(reminders, "requests", Dead([]))

    gone, problem = reminders._sweep("token", written=set())

    assert gone == 0 and "sweeping stray" in problem


def test_switching_the_apple_channel_off_sweeps_what_it_left(todoist, reminders_app):
    """A channel turned off takes its reminders with it. The sweep is the only
    thing that ever deletes them, so returning early would leave the list
    nagging about posts for good."""
    state = FakeState([Photo(file="a.jpg", caption="x")], apple=False)

    reminders.sync(state, url_for)

    assert reminders_app == [[]]  # called once, with nothing to write


def test_the_apple_channel_on_writes_a_nudge_per_post(todoist, reminders_app):
    state = FakeState([Photo(file="a.jpg"), Photo(file="b.jpg")], apple=True)

    reminders.sync(state, url_for)

    assert [n.title for n in reminders_app[0]] == [
        "Post a.jpg to Instagram", "Post b.jpg to Instagram",
    ]
