"""Phase 2 tests. The API is faked — these check our logic, not Claude's."""

from __future__ import annotations

import json
import locale
from datetime import datetime
from types import SimpleNamespace

import pytest
from PIL import Image

from src.caption import (
    FLAG_CAPTION_FAILED,
    FLAG_NO_DATE,
    FLAG_NO_PLACE,
    _validate,
    draft_caption,
)
from src.config import CaptionConfig
from src.exif import date_suffix, read_capture_date

CFG = CaptionConfig(model="claude-opus-5", max_tokens=2000, effort="low", max_chars=90)


# --- fake client -----------------------------------------------------------


def reply(caption=None, *, raw=None, stop_reason="end_turn"):
    text = raw if raw is not None else json.dumps({"caption": caption})
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
    )


class FakeClient:
    """Records requests and returns queued responses in order."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)

    def last_text(self):
        """The text block of the most recent user message."""
        content = self.calls[-1]["messages"][0]["content"]
        return next(b["text"] for b in content if b["type"] == "text")


@pytest.fixture(scope="session")
def photo(make_photo):
    """A JPEG carrying DateTimeOriginal, like a real camera file."""
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "2023:06:11 10:37:02"
    return make_photo("captioned.jpg", 900, 600, exif=exif)


@pytest.fixture(scope="session")
def dateless_photo(make_photo):
    return make_photo("dateless.jpg", 900, 600)


# --- the date bracket ------------------------------------------------------


def test_date_suffix_format():
    assert date_suffix(datetime(2024, 1, 9)) == " (Jan, 2024)"
    assert date_suffix(datetime(2025, 10, 31)) == " (Oct, 2025)"
    assert date_suffix(None) == " (?, ?)"


def test_month_name_is_english_under_dutch_locale():
    """The reason MONTHS is hardcoded: strftime would emit 'mrt' on this machine."""
    try:
        locale.setlocale(locale.LC_TIME, "nl_NL.UTF-8")
    except locale.Error:
        pytest.skip("nl_NL.UTF-8 not installed")
    try:
        when = datetime(2024, 3, 15)
        assert date_suffix(when) == " (Mar, 2024)"
        assert when.strftime("%b") != "Mar", "locale did not actually switch"
    finally:
        locale.setlocale(locale.LC_TIME, "C")


def test_reads_datetimeoriginal(photo):
    when, source = read_capture_date(photo)
    assert when == datetime(2023, 6, 11, 10, 37, 2)
    assert source == "DateTimeOriginal"


def test_missing_date_returns_none_not_today(dateless_photo):
    when, source = read_capture_date(dateless_photo)
    assert when is None and source is None


# --- style validation ------------------------------------------------------


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("", "empty"),
        ("two\nlines", "single line"),
        ("Sunset on Lamu", "lowercase"),
        ("sunset on Lamu #travel", "hashtag"),
        ("what a sunset!", "exclamation"),
        ("sunset on Lamu ☀", "emoji"),
        ("sunset on Lamu 2024", "year"),
        ("a " + "very " * 40 + "long one", "characters"),
    ],
)
def test_validate_rejects(text, fragment):
    problem = _validate(text, CFG.max_chars)
    assert problem and fragment in problem


def test_validate_accepts_house_style():
    assert _validate("a leopard crossing the last light", CFG.max_chars) is None


# --- drafting --------------------------------------------------------------


def test_place_is_passed_as_stated_fact(photo):
    client = FakeClient(reply("sunset on Lamu"))

    draft = draft_caption(photo, place="Lamu", cfg=CFG, client=client)

    assert "Lamu" in client.last_text()
    assert "confirmed" in client.last_text()
    assert draft.caption == "sunset on Lamu (Jun, 2023)"
    assert draft.flags == []


def test_no_place_instructs_the_model_to_omit_location(photo):
    client = FakeClient(reply("low cloud coming over the ridge"))

    draft = draft_caption(photo, place=None, cfg=CFG, client=client)

    assert "not known" in client.last_text()
    assert "no location" in client.last_text()
    assert draft.caption == "low cloud coming over the ridge (Jun, 2023)"
    assert draft.flags == [FLAG_NO_PLACE]


def test_missing_date_flags_and_uses_question_marks(dateless_photo):
    client = FakeClient(reply("light on the water"))

    draft = draft_caption(dateless_photo, place="Lamu", cfg=CFG, client=client)

    assert draft.caption == "light on the water (?, ?)"
    assert draft.flags == [FLAG_NO_DATE]


def test_retries_once_with_the_specific_problem(photo):
    client = FakeClient(reply("Sunset on Lamu!"), reply("sunset on Lamu"))

    draft = draft_caption(photo, place="Lamu", cfg=CFG, client=client)

    assert len(client.calls) == 2
    assert draft.caption == "sunset on Lamu (Jun, 2023)"
    assert draft.ok
    # The retry tells the model what was wrong rather than just asking again.
    correction = client.calls[1]["messages"][-1]["content"]
    assert "lowercase" in correction


def test_two_failures_fall_back_to_empty_and_flag(photo):
    client = FakeClient(reply("Nope!"), reply("Still Wrong!"))

    draft = draft_caption(photo, place="Lamu", cfg=CFG, client=client)

    assert len(client.calls) == 2
    assert draft.caption == ""
    assert FLAG_CAPTION_FAILED in draft.flags
    assert draft.ok is False


def test_malformed_json_is_retried(photo):
    client = FakeClient(reply(raw="not json at all"), reply("sunset on Lamu"))

    draft = draft_caption(photo, place="Lamu", cfg=CFG, client=client)

    assert draft.caption == "sunset on Lamu (Jun, 2023)"


def test_refusal_flags_without_retrying(photo):
    client = FakeClient(reply(raw="", stop_reason="refusal"))

    draft = draft_caption(photo, place="Lamu", cfg=CFG, client=client)

    assert len(client.calls) == 1
    assert FLAG_CAPTION_FAILED in draft.flags


def test_trailing_period_is_stripped(photo):
    client = FakeClient(reply("sunset on Lamu."))

    draft = draft_caption(photo, place="Lamu", cfg=CFG, client=client)

    assert draft.caption == "sunset on Lamu (Jun, 2023)"


def test_request_shape(photo):
    """Opus 5 rejects temperature; the schema is what guarantees parseable JSON."""
    client = FakeClient(reply("sunset on Lamu"))

    draft_caption(photo, place="Lamu", cfg=CFG, client=client)

    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert "temperature" not in call
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "low"
    image = call["messages"][0]["content"][0]
    assert image["type"] == "image"
    assert image["source"]["media_type"] == "image/jpeg"
