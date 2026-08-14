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

from datetime import datetime

import requests
from requests import RequestException

from src.config import secret

API = "https://api.todoist.com/api/v1/tasks"
TIMEOUT = 30


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _body(photo, when: datetime, urls: list[str]) -> dict:
    caption = photo.caption.strip() or "(no caption yet)"
    lines = [caption, ""]
    lines += urls
    if photo.is_carousel:
        lines.append(f"\nCarousel of {len(photo.files)}, in this order.")
    if not photo.caption.strip():
        lines.append("\nNo caption written yet.")
    return {
        "content": f"Post {photo.file} to Instagram",
        "description": "\n".join(lines),
        "due_datetime": when.isoformat(),
        # Todoist's own notification for a timed task. Ignored on plans without
        # reminders; the task still lands in Today with the time on it.
        "auto_reminder": True,
    }


def sync(state, image_url) -> list[str]:
    """Create, update, and clear the reminders for the queue. Returns log lines.

    `image_url` is passed in rather than imported so this module doesn't drag in
    the publisher (and its git calls) just to build a URL.
    """
    cfg = state.cfg
    if not cfg.publish.reminders:
        return []

    token = secret("TODOIST_API_TOKEN", required=False)
    if not token:
        return ["reminders skipped (no TODOIST_API_TOKEN in .env)"]

    from app.state import STATUS_READY

    ready = [p for p in state.ordered() if p.status == STATUS_READY]
    upcoming = ready[: cfg.publish.reminder_count]
    slots = state.upcoming(len(upcoming))

    log: list[str] = []
    created = updated = removed = 0

    for photo, when in zip(upcoming, slots):
        urls = [image_url(state.processed_path(f).name, cfg) for f in photo.files]
        body = _body(photo, when, urls)
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
    log.insert(0, f"reminders: {', '.join(parts)}")
    return log
