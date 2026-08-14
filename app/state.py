"""Working state for the curation app.

queue.yaml is the committed artifact, written only on Save. This file is the
scratchpad in between: it survives restarts so a session's drafted captions and
place labels aren't lost by closing the tab. It is git-ignored.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
    # The three drafted alternatives, keyed by voice. Working state only — the
    # committed queue carries the one caption that was actually chosen.
    caption_options: dict[str, str] = field(default_factory=dict)

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

    def _jitter(self, base: datetime) -> timedelta:
        """A stable ± offset for one slot, derived from the slot itself.

        Deterministic on purpose. A random offset per call would make the times
        in the app dance on every refresh and disagree with what the publisher
        eventually does; hashing the nominal timestamp gives an offset that is
        scattered across slots but identical every time anyone asks about *this*
        slot. blake2b rather than hash(), which is salted per process.
        """
        spread = self.cfg.schedule.jitter_minutes * 60
        if spread <= 0:
            return timedelta()
        digest = hashlib.blake2b(base.isoformat().encode(), digest_size=8).digest()
        return timedelta(seconds=int.from_bytes(digest, "big") % (2 * spread + 1) - spread)

    def upcoming(self, count: int) -> list[datetime]:
        """The next `count` posting times, in UTC, jittered and in order.

        Slots are configured as local wall-clock times because the target is a
        human attention window; the conversion to UTC happens here, so the hour
        stays put across a DST change instead of sliding by one.
        """
        tz = ZoneInfo(self.cfg.schedule.timezone)
        now = datetime.now(timezone.utc)
        local = now.astimezone(tz)
        # Midnight Monday of the current local week — arithmetic from there is
        # wall-clock, which is what "Wednesday 11:30" is supposed to mean.
        monday = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=local.weekday()
        )

        out: list[datetime] = []
        week = 0
        while len(out) < count and week < 200:
            for slot in self.cfg.schedule.slots:
                base = (monday + timedelta(days=7 * week + slot.weekday)).replace(
                    hour=slot.hour, minute=slot.minute
                )
                when = (base + self._jitter(base)).astimezone(timezone.utc)
                if when > now:
                    out.append(when)
            week += 1
        out.sort()
        return out[:count]

    def slot(self, n: int) -> datetime:
        """UTC time of the nth upcoming posting slot (0 = the next one)."""
        return self.upcoming(n + 1)[n]

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
        ready = [n for n in self.order if self.photos[n].status == STATUS_READY]
        slots = {
            name: when.isoformat()
            for name, when in zip(ready, self.upcoming(len(ready)))
        }

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
            "caption_enabled": self.cfg.caption.enabled,
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
