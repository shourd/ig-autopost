"""Todoist reminders for posts Sjoerd wants to publish by hand.

The pipeline can publish on its own, but until that's trusted the useful thing
is a nudge at the right moment: a task due at the slot the post was scheduled
for, carrying the caption and a link to the image, so the phone can do the
reminding and the posting stays manual.

Reminders are rewritten on every Save rather than appended to, because the
schedule moves whenever the queue is reordered — a stale "post this on Sunday"
is worse than no reminder at all. The photo's task id is stored in the working
state, so the same task is updated instead of a second one appearing.

Nothing here is ever allowed to raise. A Todoist outage must not stop a Save.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from requests import RequestException

from src.config import secret

API = "https://api.todoist.com/api/v1/tasks"
ALARMS = "https://api.todoist.com/api/v1/reminders"
TIMEOUT = 30


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _body(photo, when: datetime, urls: list[str], tz: ZoneInfo) -> dict:
    caption = photo.caption.strip() or "(no caption yet)"
    # The notification arrives ahead of the slot, so the slot has to be written
    # down — in local time, which is the only one worth reading on a phone.
    local = when.astimezone(tz).strftime("%a %-d %b, %H:%M")
    lines = [f"Post at {local}.", "", caption, ""]
    lines += urls
    if photo.is_carousel:
        lines.append(f"\nCarousel of {len(photo.files)}, in this order.")
    if not photo.caption.strip():
        lines.append("\nNo caption written yet.")
    from src.apple_reminders import title_for

    return {
        # One title for both channels, so the Reminders sweep recognises its own.
        "content": title_for(photo.file),
        "description": "\n".join(lines),
        "due_datetime": when.isoformat(),
        # The push comes from an explicit reminder set `reminder_lead_minutes`
        # ahead (see `_alarm`), not from Todoist's default one at the due time.
        "auto_reminder": False,
    }


def _alarm(token: str, task_id: str, lead: int) -> str | None:
    """Ensure a push reminder sits `lead` minutes before the task's due time.

    Relative reminders track the due date, so this only has to be created once —
    reordering the queue moves the task and the reminder follows. Existing ones
    are looked up rather than tracked in state, which keeps the app honest if a
    reminder is deleted in Todoist.

    Returns a log line on failure, None on success. Todoist reminders need a
    paid plan; on a free one this is the call that says so.
    """
    if lead <= 0:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(
            ALARMS, headers=headers, params={"task_id": task_id}, timeout=TIMEOUT
        )
        resp.raise_for_status()
        payload = resp.json()
        existing = payload.get("results", []) if isinstance(payload, dict) else payload
        if any(alarm.get("minute_offset") == lead for alarm in existing):
            return None

        resp = requests.post(
            ALARMS,
            headers=headers,
            json={
                "task_id": task_id,
                "reminder_type": "relative",
                "minute_offset": lead,
                "service": "push",
            },
            timeout=TIMEOUT,
        )
        if resp.status_code in (402, 403):
            return (
                "! phone reminders need a paid Todoist plan — the tasks are there "
                "with their times, but Todoist won't push a notification"
            )
        resp.raise_for_status()
    except (RequestException, ValueError) as exc:
        return f"! reminder alarm failed: {exc}"
    return None


def sync(state, image_url) -> list[str]:
    """Create, update, and clear the reminders for the queue. Returns log lines.

    Two channels, one list of posts: Todoist carries the caption and the links
    and is the written record; the Apple reminder is what actually reaches the
    phone, since Todoist's own alarms need a paid plan.

    `image_url` is passed in rather than imported so this module doesn't drag in
    the publisher (and its git calls) just to build a URL.
    """
    cfg = state.cfg
    if not cfg.publish.reminders:
        return []

    from app.state import STATUS_READY

    ready = [p for p in state.ordered() if p.status == STATUS_READY]
    upcoming = ready[: cfg.publish.reminder_count]
    slots = state.upcoming(len(upcoming))
    tz = ZoneInfo(cfg.schedule.timezone)

    posts = [
        (photo, when, _body(photo, when, [
            image_url(state.processed_path(f).name, cfg) for f in photo.files
        ], tz))
        for photo, when in zip(upcoming, slots)
    ]

    log = _todoist(state, posts, cfg)
    log += _apple(posts, cfg)
    return log


def _apple(posts, cfg) -> list[str]:
    """Mirror the posts into the Reminders app, alarm set `lead` ahead."""
    if not cfg.publish.reminder_apple:
        return []

    from src.apple_reminders import Nudge, sync as apple_sync

    lead = timedelta(minutes=cfg.publish.reminder_lead_minutes)
    nudges = [
        Nudge(title=body["content"], body=body["description"], when=when - lead)
        for _, when, body in posts
    ]
    return apple_sync(nudges, cfg.publish.reminder_apple_list)


def _todoist(state, posts, cfg) -> list[str]:
    token = secret("TODOIST_API_TOKEN", required=False)
    if not token:
        return ["Todoist skipped (no TODOIST_API_TOKEN in .env)"]

    log: list[str] = []
    created = updated = removed = 0
    upcoming = [photo for photo, _, _ in posts]

    for photo, _, body in posts:
        try:
            if photo.reminder_id:
                resp = requests.post(
                    f"{API}/{photo.reminder_id}",
                    headers=_headers(token), json=body, timeout=TIMEOUT,
                )
                if resp.status_code == 404:  # deleted in Todoist; make a new one
                    photo.reminder_id = None
                else:
                    resp.raise_for_status()
                    updated += 1
            if not photo.reminder_id:
                resp = requests.post(
                    API, headers=_headers(token), json=body, timeout=TIMEOUT
                )
                resp.raise_for_status()
                photo.reminder_id = str(resp.json()["id"])
                created += 1
        except (RequestException, KeyError, ValueError) as exc:
            log.append(f"! reminder for {photo.file} failed: {exc}")
            continue

        # Only worth trying when Todoist is the notifying channel; with Apple
        # Reminders on, a premium warning every Save is just noise.
        if not cfg.publish.reminder_apple:
            problem = _alarm(token, photo.reminder_id, cfg.publish.reminder_lead_minutes)
            if problem and problem not in log:  # one plan warning is plenty
                log.append(problem)

    # Anything no longer in the window — posted, held, pushed down the queue —
    # loses its task, so Todoist never says "post this" about a live post.
    keep = {p.file for p in upcoming}
    for photo in state.photos.values():
        if photo.file in keep or not photo.reminder_id:
            continue
        try:
            resp = requests.delete(
                f"{API}/{photo.reminder_id}", headers=_headers(token), timeout=TIMEOUT
            )
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()
            removed += 1
        except RequestException as exc:
            log.append(f"! clearing reminder for {photo.file} failed: {exc}")
        photo.reminder_id = None

    parts = [f"{created} new", f"{updated} updated", f"{removed} cleared"]
    log.insert(0, f"Todoist: {', '.join(parts)}")
    return log


def main() -> None:
    """`uv run python -m src.reminders` — rewrite the reminders without a Save.

    Mostly for the first run: macOS asks permission to control Reminders the
    first time, and that dialog wants a terminal someone is actually looking at.
    """
    from app.state import State
    from src.publish import image_url

    state = State()
    for line in sync(state, image_url):
        print(f"  {line}")
    state.save()


if __name__ == "__main__":
    main()
