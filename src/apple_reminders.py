"""Reminders in the macOS Reminders app, which is what actually reaches the phone.

Todoist gates timed reminders behind a paid plan (`403 PREMIUM_ONLY`), so the
Todoist task is the written record and this is the alarm: a reminder with a
`remind me date` set ahead of the slot, synced to the iPhone by iCloud like any
other. Free, and it needs no network at all.

Two things make this fiddly enough to deserve its own module:

  1. AppleScript dates cannot be parsed from a string without knowing the
     machine's date format, so the target time is built by adding a Unix
     timestamp to a date object pinned at the epoch.
  2. There is no id to hold on to across runs — or rather, holding one would go
     stale the moment a reminder is deleted by hand. Reminders are addressed by
     title instead, and rewritten rather than updated.

Nothing here raises. macOS will ask for permission to control Reminders the
first time; if that is refused, it is reported and the Save carries on.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

TIMEOUT = 30
# osascript's error number when Automation permission has been refused.
NOT_AUTHORISED = "-1743"
WAITING = "permission dialog unanswered"


@dataclass(frozen=True)
class Nudge:
    title: str
    body: str
    when: datetime


def _quote(text: str) -> str:
    """Escape a Python string into an AppleScript string literal."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return escaped.replace("\n", "\\n").replace("\r", "")


def _script(nudges: list[Nudge], list_name: str | None) -> str:
    # `current date` minus the current epoch gives a date object sitting at the
    # epoch in local time; adding a timestamp to it lands on the right instant
    # without ever formatting or parsing a date string.
    lines = [
        'set epochStart to (current date) - ((do shell script "date +%s") as integer)',
        'tell application "Reminders"',
    ]
    if list_name:
        lines += [
            "\ttry",
            f'\t\tset theList to first list whose name is "{_quote(list_name)}"',
            "\ton error",
            "\t\tset theList to default list",
            "\tend try",
        ]
    else:
        lines.append("\tset theList to default list")

    for nudge in nudges:
        title = _quote(nudge.title)
        stamp = int(nudge.when.replace(tzinfo=nudge.when.tzinfo or timezone.utc).timestamp())
        lines += [
            # Rewritten, not updated: the schedule moves whenever the queue does.
            f'\tdelete (every reminder of theList whose name is "{title}" '
            "and completed is false)",
            f"\tset theDate to epochStart + {stamp}",
            f'\tmake new reminder at end of theList with properties '
            f'{{name:"{title}", body:"{_quote(nudge.body)}", remind me date:theDate}}',
        ]

    lines.append("end tell")
    return "\n".join(lines)


def _run(script: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["osascript", "-"], input=script, capture_output=True, text=True, timeout=TIMEOUT
        )
    except subprocess.TimeoutExpired:
        # Almost always the permission dialog sitting unanswered — osascript
        # blocks on it, so a timeout here means "nobody clicked Allow".
        return False, WAITING
    except OSError as exc:  # no osascript at all
        return False, str(exc)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def sync(nudges: list[Nudge], list_name: str | None = None, run=_run) -> list[str]:
    """Put each nudge in the Reminders app. Returns log lines."""
    if not nudges:
        return []
    if sys.platform != "darwin":
        return ["! Apple Reminders skipped (not macOS)"]

    ok, output = run(_script(nudges, list_name))
    if ok:
        where = f'"{list_name}"' if list_name else "the default list"
        return [f"Apple Reminders: {len(nudges)} in {where}"]
    if WAITING in output:
        return [
            "! Apple Reminders: macOS is asking permission to control Reminders — "
            "answer the dialog on screen, then Save again."
        ]
    if NOT_AUTHORISED in output:
        return [
            "! Apple Reminders needs permission: System Settings → Privacy & "
            "Security → Automation, and allow Reminders for the terminal running "
            "this app. Nothing else is affected."
        ]
    return [f"! Apple Reminders failed: {output.splitlines()[-1] if output else 'unknown'}"]
