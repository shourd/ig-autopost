"""The Reminders-app channel: the one that actually reaches the phone.

The AppleScript is generated, never parsed back, so these tests read it — an
escaping mistake here is a syntax error at Save time on someone else's machine.
"""

from __future__ import annotations

from datetime import datetime, timezone

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.apple_reminders import Nudge, _script, sync, title_for

WHEN = datetime(2026, 8, 19, 9, 52, tzinfo=timezone.utc)
NUDGE = Nudge(title="Post a.jpg to Instagram", body="Post at Wed 19 Aug, 11:52.", when=WHEN)


def test_the_time_is_built_from_a_timestamp_not_a_date_string():
    """AppleScript date parsing depends on the machine's date format; this can't."""
    script = _script([NUDGE], None)

    assert 'set epochStart to (current date) - ((do shell script "date +%s") as integer)' in script
    assert f"set theDate to epochStart + {int(WHEN.timestamp())}" in script
    assert "remind me date:theDate" in script


def test_every_reminder_this_app_made_is_swept_before_writing():
    """A photo dropped from the queue must stop buzzing, not just get rewritten."""
    script = _script([NUDGE], None)

    assert (
        'delete (every reminder of theList whose completed is false '
        'and name starts with "Post " and name ends with " to Instagram")'
    ) in script
    assert script.index("delete (every reminder") < script.index("make new reminder")


def test_an_empty_queue_still_sweeps():
    script = _script([], None)

    assert "delete (every reminder" in script
    assert "make new reminder" not in script


def test_the_default_list_is_used_when_none_is_named():
    assert "set theList to default list" in _script([NUDGE], None)


def test_a_named_list_falls_back_to_the_default_if_it_is_missing():
    script = _script([NUDGE], "Instagram")

    assert 'first list whose name is "Instagram"' in script
    assert "on error" in script and "set theList to default list" in script


def test_quotes_and_newlines_are_escaped():
    nudge = Nudge(title='He said "go"', body="one\ntwo\\three", when=WHEN)

    script = _script([nudge], None)

    assert r'name:"He said \"go\""' in script
    assert r"one\ntwo\\three" in script
    # A raw newline inside a literal would be a syntax error in AppleScript.
    assert "\n" not in script.split("body:")[1].split(", remind me date")[0]


def test_every_nudge_lands_in_one_script():
    """One osascript call, so macOS asks for permission once, not per photo."""
    other = Nudge(title="Post b.jpg to Instagram", body="later", when=WHEN)

    script = _script([NUDGE, other], None)

    assert script.count("make new reminder") == 2
    assert script.count("tell application") == 1


def test_a_refused_permission_says_what_to_do():
    def refuse(_script):
        return False, "execution error: Not authorized to send Apple events to Reminders. (-1743)"

    log = sync([NUDGE], None, run=refuse)

    assert len(log) == 1
    assert "Automation" in log[0] and "permission" in log[0]


def test_an_unanswered_dialog_says_to_answer_it():
    """osascript blocks on the permission prompt, so a timeout means exactly that."""
    from src.apple_reminders import WAITING

    log = sync([NUDGE], None, run=lambda _s: (False, WAITING))

    assert "answer the dialog" in log[0]


def test_other_failures_are_reported_not_raised():
    log = sync([NUDGE], None, run=lambda _s: (False, "Reminders got an error: boom"))

    assert log == ["! Apple Reminders failed: Reminders got an error: boom"]


def test_success_names_the_list():
    log = sync([NUDGE], "Instagram", run=lambda _s: (True, ""))

    assert log == ['Apple Reminders: 1 in "Instagram"']


def test_an_emptied_queue_reports_the_sweep():
    log = sync([], None, run=lambda _s: (True, ""))

    assert log == ["Apple Reminders: 0 in the default list"]


def test_the_title_template_is_shared_with_the_todoist_task():
    """The sweep recognises its own reminders by title; one template, two users."""
    from src.reminders import _body

    task = _body(SimpleNamespace(file="a.jpg", caption="x", is_carousel=False, files=["a.jpg"]),
                 WHEN, [], ZoneInfo("Europe/Amsterdam"))

    assert task["content"] == title_for("a.jpg")
