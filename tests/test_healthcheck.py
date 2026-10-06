"""The weekly check: what counts as broken, and how it says so."""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from src import healthcheck
from src.apple_reminders import ALERT_TITLE, _alert_script, alert

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


@pytest.fixture
def issued(monkeypatch):
    """Pin what the .env says the token's issue date is."""
    def set_to(value):
        monkeypatch.setattr(healthcheck, "secret",
                            lambda name, required=True: value if name == "META_TOKEN_ISSUED" else None)
    return set_to


# --- token age -------------------------------------------------------------


def test_a_fresh_token_passes(issued):
    issued((NOW - timedelta(days=3)).isoformat())

    check = healthcheck.check_token_age(NOW)

    assert check.ok and "57d left" in check.detail


def test_the_last_ten_days_are_a_warning(issued):
    """Three posting slots of notice: enough to re-authorise without a gap."""
    issued((NOW - timedelta(days=55)).isoformat())

    check = healthcheck.check_token_age(NOW)

    assert not check.ok
    assert "expires in 5d" in check.detail
    assert "token_setup" in check.fix


def test_an_expired_token_says_how_long_ago(issued):
    issued((NOW - timedelta(days=70)).isoformat())

    check = healthcheck.check_token_age(NOW)

    assert not check.ok and "expired 10d ago" in check.detail


def test_an_unrecorded_issue_date_is_not_a_failure(issued):
    """Tokens from before the date was written down mustn't nag every week."""
    issued(None)

    assert healthcheck.check_token_age(NOW).ok


# --- queue -----------------------------------------------------------------


def fake_state(statuses, slots=3):
    photos = [types.SimpleNamespace(status=s, file=f"{i}.jpg") for i, s in enumerate(statuses)]
    return types.SimpleNamespace(
        ordered=lambda: photos,
        cfg=types.SimpleNamespace(schedule=types.SimpleNamespace(slots=[0] * slots)),
    )


def test_an_empty_queue_is_a_failure():
    check = healthcheck.check_queue(fake_state(["posted", "posted"]))

    assert not check.ok and "nothing ready" in check.detail


def test_a_queue_down_to_a_week_is_a_failure():
    """Running out is as fatal as a dead token, and just as quiet."""
    check = healthcheck.check_queue(fake_state(["ready"] * 3))

    assert not check.ok and "3 post(s) left" in check.detail


def test_a_full_queue_reports_how_long_it_lasts():
    check = healthcheck.check_queue(fake_state(["ready"] * 30))

    assert check.ok and "10 weeks" in check.detail


# --- reminders -------------------------------------------------------------


def test_a_reminder_failure_is_reported_as_one(monkeypatch):
    monkeypatch.setattr("src.reminders.sync", lambda *a: ["! Apple Reminders failed: nope"])

    checks = healthcheck.refresh_reminders(fake_state([]))

    assert [c.ok for c in checks] == [False]
    assert "nope" in checks[0].detail


def test_both_channels_are_reported_separately(monkeypatch):
    """Todoist can be down while the alarm that matters is fine."""
    monkeypatch.setattr("src.reminders.sync",
                        lambda *a: ["! Todoist failed", "Apple Reminders: 4 in the default list"])

    checks = healthcheck.refresh_reminders(fake_state([]))

    assert [c.ok for c in checks] == [False, True]


def test_a_crash_in_the_sync_does_not_take_the_check_down(monkeypatch):
    def boom(*a):
        raise RuntimeError("kaboom")
    monkeypatch.setattr("src.reminders.sync", boom)

    checks = healthcheck.refresh_reminders(fake_state([]))

    assert not checks[0].ok and "kaboom" in checks[0].detail


# --- the alert reminder ----------------------------------------------------


def test_the_alert_is_swept_before_it_is_written():
    script = _alert_script("something broke", None)

    assert script.index("delete") < script.index("make new reminder")
    assert ALERT_TITLE in script


def test_a_clean_run_only_sweeps():
    """Once the problem is fixed the phone must stop insisting on it."""
    script = _alert_script(None, None)

    assert "delete" in script and "make new reminder" not in script


def test_the_alert_title_survives_the_post_nudge_sweep():
    """The two sweeps share a list, so neither may match the other's titles."""
    from src.apple_reminders import TITLE_PREFIX, TITLE_SUFFIX

    assert not (ALERT_TITLE.startswith(TITLE_PREFIX) and ALERT_TITLE.endswith(TITLE_SUFFIX))


def test_the_raised_alert_is_reported_by_name(monkeypatch):
    monkeypatch.setattr("src.apple_reminders.sys", types.SimpleNamespace(platform="darwin"))
    seen = []

    out = alert("something broke", None, run=lambda s: (seen.append(s), (True, ""))[1])

    assert ALERT_TITLE in seen[0]
    assert out == [f'Reminders alert: "{ALERT_TITLE}" raised']
