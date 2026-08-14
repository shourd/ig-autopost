"""Working state for the curation app.

queue.yaml is the committed artifact, written only on Save. This file is the
scratchpad in between: it survives restarts so a session's drafted captions and
place labels aren't lost by closing the tab. It is git-ignored.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src.config import REPO_ROOT, load_config

STATE_PATH = REPO_ROOT / ".curation.json"

STATUS_READY = "ready"
STATUS_HOLD = "hold"
STATUS_POSTED = "posted"

# Files that live alongside the backfilled posts but aren't posts themselves.
RESERVED = {"avatar.jpg"}


@dataclass
class Photo:
    file: str
    caption: str = ""
    caption_reviewed: bool = False
    place: str | None = None
    location_id: str | None = None
    status: str = STATUS_READY
    flags: list[str] = field(default_factory=list)
    drafted: bool = False
    upscale_blocked: bool = False
    date: str | None = None

    def queue_entry(self) -> dict:
        """The committed shape, exactly as specified."""
        return {
            "file": self.file,
            "caption": self.caption,
            "caption_reviewed": self.caption_reviewed,
            "place": self.place,
            "location_id": self.location_id,
            "status": self.status,
        }


class State:
    """Photo metadata keyed by filename, plus the posting order."""

    def __init__(self) -> None:
        self.cfg = load_config()
        self.order: list[str] = []
        self.photos: dict[str, Photo] = {}
        self._load()

    # --- already-published grid --------------------------------------------

    def posted(self) -> list[str]:
        """Everything already live on Instagram, newest first — grid order.

        Two sources, deliberately kept apart on disk but merged here:
        photos/posted/ is what this pipeline published (committed, because the
        publisher's move is a git diff), photos/history/ is the backfill from
        the API (git-ignored, display-only). The grid doesn't care which is
        which — both are simply "already posted".

        Ordering is by mtime, which `src.history` stamps to each post's real
        publish time so the backfilled grid matches Instagram's own.
        """
        files = [
            p
            for folder in (self.cfg.paths.posted, self.cfg.paths.history)
            if folder.is_dir()
            for p in folder.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg"}
            and not p.name.startswith(".")
            and p.name not in RESERVED  # sidecars, not posts
        ]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [p.name for p in files]

    def posted_path(self, name: str) -> Path | None:
        """Locate an already-posted image across both folders."""
        for folder in (self.cfg.paths.posted, self.cfg.paths.history):
            candidate = folder / name
            if candidate.is_file():
                return candidate
        return None

    # --- posting schedule --------------------------------------------------

    def slot(self, n: int) -> datetime:
        """UTC time of the nth upcoming posting slot (0 = the next one).

        Mirrors the Actions cron, so what the app shows is what will happen.
        """
        now = datetime.now(timezone.utc)
        days_ahead = (self.cfg.schedule.weekday - now.weekday()) % 7
        slot = (now + timedelta(days=days_ahead)).replace(
            hour=self.cfg.schedule.hour_utc, minute=0, second=0, microsecond=0
        )
        if slot <= now:
            slot += timedelta(days=7)
        return slot + timedelta(days=7 * n)

    # --- persistence -------------------------------------------------------

    def _load(self) -> None:
        if STATE_PATH.exists():
            raw = json.loads(STATE_PATH.read_text())
            self.order = raw.get("order", [])
            self.photos = {k: Photo(**v) for k, v in raw.get("photos", {}).items()}
        elif self.cfg.paths.queue.exists():
            # First run after a clone: seed from the committed queue.
            for entry in yaml.safe_load(self.cfg.paths.queue.read_text()) or []:
                photo = Photo(**{k: entry[k] for k in Photo.__annotations__ if k in entry})
                photo.drafted = bool(photo.caption)
                self.photos[photo.file] = photo
                self.order.append(photo.file)
        self.rescan()

    def save(self) -> None:
        STATE_PATH.write_text(
            json.dumps(
                {"order": self.order, "photos": {k: asdict(v) for k, v in self.photos.items()}},
                indent=2,
            )
        )

    # --- the folder is the source of truth ---------------------------------

    def rescan(self) -> None:
        """Pick up newly dropped files; forget ones that are gone."""
        on_disk = {
            p.name
            for p in self.cfg.paths.raw.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg"} and not p.name.startswith(".")
        }
        for name in sorted(on_disk - set(self.photos)):
            self.photos[name] = Photo(file=name)
            self.order.append(name)
        for name in set(self.photos) - on_disk:
            if self.photos[name].status != STATUS_POSTED:
                del self.photos[name]
        self.order = [n for n in self.order if n in self.photos]
        self.order += [n for n in self.photos if n not in self.order]

    # --- accessors ---------------------------------------------------------

    def ordered(self) -> list[Photo]:
        return [self.photos[n] for n in self.order]

    def raw_path(self, name: str) -> Path:
        return self.cfg.paths.raw / name

    def processed_path(self, name: str) -> Path:
        return self.cfg.paths.processed / f"{Path(name).stem}.jpg"

    def as_json(self) -> dict:
        # Held photos are skipped, so they don't consume a posting slot.
        slots: dict[str, str] = {}
        n = 0
        for name in self.order:
            if self.photos[name].status == STATUS_READY:
                slots[name] = self.slot(n).isoformat()
                n += 1

        posted = self.posted()
        p = self.cfg.profile
        # The API is authoritative when src.history has run; config.yaml is the
        # fallback for a fresh clone with no token. Typed-in values describe
        # whichever account someone screenshotted, which is how this header
        # spent a while showing the wrong profile entirely.
        live = self.live_profile()
        return {
            "order": self.order,
            "photos": {k: asdict(v) for k, v in self.photos.items()},
            "posted": posted,
            "slots": slots,
            "profile": {
                "username": live.get("username", p.username),
                "display_name": live.get("name", p.display_name),
                "bio": live.get("biography", p.bio),
                "posts": live.get("media_count", p.posts if p.posts is not None else len(posted)),
                "followers": live.get("followers_count", p.followers),
                "following": live.get("follows_count", p.following),
                "highlights": p.highlights,
                "has_avatar": bool(live.get("avatar") or p.avatar),
                "live": bool(live),
            },
        }

    def live_profile(self) -> dict:
        """Profile cached by `src.history`, or {} if it has never run."""
        cached = self.cfg.paths.history / "profile.yaml"
        if not cached.is_file():
            return {}
        return yaml.safe_load(cached.read_text()) or {}

    def avatar_path(self) -> Path | None:
        for candidate in (
            self.cfg.paths.history / "avatar.jpg",
            (REPO_ROOT / self.cfg.profile.avatar) if self.cfg.profile.avatar else None,
        ):
            if candidate and candidate.is_file():
                return candidate
        return None
