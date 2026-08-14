"""Posting slots: local wall-clock, jittered, deterministic."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.state import State
from src.config import ScheduleConfig, Slot

TZ = "Europe/Amsterdam"


def make_state(jitter: int = 0, slots=((2, 11, 30), (6, 10, 30))) -> State:
    state = State()
    schedule = ScheduleConfig(
        timezone=TZ,
        jitter_minutes=jitter,
        slots=tuple(Slot(*s) for s in slots),
    )
    state.cfg = dataclasses.replace(state.cfg, schedule=schedule)
    return state


# --- config ---------------------------------------------------------------


def test_slots_are_sorted_regardless_of_config_order():
    cfg = ScheduleConfig.from_raw(
        {
            "timezone": TZ,
            "jitter_minutes": 10,
            "slots": [{"weekday": 6, "hour": 10}, {"weekday": 2, "hour": 11, "minute": 30}],
        }
    )
    assert [s.weekday for s in cfg.slots] == [2, 6]
    assert cfg.slots[0].minute == 30 and cfg.slots[1].minute == 0


def test_empty_slot_list_is_rejected():
    with pytest.raises(ValueError):
        ScheduleConfig.from_raw({"timezone": TZ, "jitter_minutes": 0, "slots": []})


# --- generated times ------------------------------------------------------


def test_slots_are_future_ordered_and_utc():
    times = make_state().upcoming(6)

    assert len(times) == 6
    assert times == sorted(times)
    assert all(t.tzinfo == timezone.utc for t in times)
    assert all(t > datetime.now(timezone.utc) for t in times)


def test_the_wall_clock_hour_survives_dst():
    """The whole reason slots are local: 11:30 must stay 11:30 in November."""
    # Far enough ahead to cross the October changeover from CEST to CET.
    times = make_state().upcoming(40)
    local = [t.astimezone(ZoneInfo(TZ)) for t in times]
    wednesdays = [t for t in local if t.weekday() == 2]

    assert len(wednesdays) > 10
    assert {(t.hour, t.minute) for t in wednesdays} == {(11, 30)}
    # And the run really does span both offsets, or it proves nothing.
    assert len({t.utcoffset() for t in local}) == 2


def test_jitter_stays_inside_its_bounds():
    state = make_state(jitter=25)
    plain = make_state().upcoming(10)

    for jittered, nominal in zip(state.upcoming(10), plain):
        assert abs(jittered - nominal) <= timedelta(minutes=25)


def test_jitter_is_stable_across_calls_and_processes():
    """It has to be: the app shows a time the publisher must later agree with."""
    first = make_state(jitter=25).upcoming(8)
    second = make_state(jitter=25).upcoming(8)

    assert first == second
    # Not all identical offsets either — that would be a constant, not jitter.
    plain = make_state().upcoming(8)
    assert len({a - b for a, b in zip(first, plain)}) > 1


def test_slot_indexes_into_the_same_sequence():
    state = make_state(jitter=25)
    times = state.upcoming(3)

    assert [state.slot(n) for n in range(3)] == times
