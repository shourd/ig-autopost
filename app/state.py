"""Working state for the curation app.

queue.yaml is the committed artifact, written only on Save. This file is the
scratchpad in between: it survives restarts so a session's drafted captions and
place labels aren't lost by closing the tab. It is git-ignored.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections.abc import Iterable
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

# Carousels are declared by filename: _DSF1234A.jpg, _DSF1234B.jpg go out as one
# post. A trailing letter only groups when at least one sibling shares the stem,
# so a lone file that happens to end in a letter stays a single post.
CAROUSEL_SUFFIX = re.compile(r"^(?P<base>.+?)(?P<letter>[A-J])$")
CAROUSEL_LETTERS = "ABCDEFGHIJ"
CAROUSEL_MAX = len(CAROUSEL_LETTERS)  # Meta's limit on children per carousel


def carousel_groups(names: Iterable[str]) -> dict[str, list[str]]:
    """Map each carousel's lead file to the rest of its photos, in order.

    Files that aren't part of a carousel don't appear at all. The lead is the
    alphabetically first member, which for A/B/C naming is the A.
    """
    buckets: dict[str, list[str]] = {}
    for name in names:
        match = CAROUSEL_SUFFIX.fullmatch(Path(name).stem)
        if match:
            buckets.setdefault(match["base"], []).append(name)
    return {
        members[0]: members[1:CAROUSEL_MAX]
        for members in (sorted(v) for v in buckets.values())
        if len(members) > 1
    }


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
    # The rest of a carousel, if this photo leads one. Derived from filenames on
    # every rescan, never edited by hand.
    extra: list[str] = field(default_factory=list)
    # Todoist task reminding Sjoerd to post this one by hand, if any.
    reminder_id: str | None = None

    @property
    def files(self) -> list[str]:
        """Every photo in this post, in the order Instagram will show them."""
        return [self.file, *self.extra]

    @property
    def is_carousel(self) -> bool:
        return bool(self.extra)

    def queue_entry(self) -> dict:
        """The committed shape, exactly as specified.

        A carousel adds `files`; `file` stays the lead so anything reading the
        queue for a single image still finds one.
        """
        entry = {
            "file": self.file,
            "caption": self.caption,
            "caption_reviewed": self.caption_reviewed,
            "place": self.place,
            "location_id": self.location_id,
            "status": self.status,
        }
        if self.extra:
            entry["files"] = self.files
        return entry


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
        names = [p.name for p in files]
        # A published carousel occupies one square on the profile, so it gets
        # one cell here too — same rule as the queue above it.
        followers = {n for members in carousel_groups(names).values() for n in members}
        return [n for n in names if n not in followers]

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
        # A carousel is one post, so only its lead file gets a Photo; the rest
        # ride along in `extra`. Recomputed every scan, because renaming a file
        # is how you change your mind about the grouping.
        groups = carousel_groups(on_disk)
        followers = {name for members in groups.values() for name in members}
        leads = on_disk - followers

        for name in sorted(leads - set(self.photos)):
            self.photos[name] = Photo(file=name)
            self.order.append(name)
        for name in set(self.photos) - leads:
            if self.photos[name].status != STATUS_POSTED:
                del self.photos[name]
        for name, photo in self.photos.items():
            if name in leads:
                photo.extra = groups.get(name, [])
        self.order = [n for n in self.order if n in self.photos]
        self.order += [n for n in self.photos if n not in self.order]

    # --- accessors ---------------------------------------------------------

    def ordered(self) -> list[Photo]:
        return [self.photos[n] for n in self.order]

    def remove(self, name: str) -> list[str]:
        """Take a post out of the queue by moving its photos aside.

        Moved, not deleted: `photos/removed/` is a holding pen, and putting a
        photo back is a drag into `photos/raw/`. The processed render goes for
        real, since it is derived and would be rebuilt anyway. A carousel leaves
        as a unit — its frames are one post, and half a post is not a thing.

        Returns the filenames moved. Raises KeyError if it isn't in the queue.
        """
        photo = self.photos[name]
        self.cfg.paths.removed.mkdir(parents=True, exist_ok=True)

        moved = []
        for member in photo.files:
            src = self.raw_path(member)
            if src.is_file():
                dst = self.cfg.paths.removed / member
                # Never clobber something already sitting in the holding pen.
                if dst.exists():
                    dst = dst.with_name(f"{dst.stem}-{int(time.time())}{dst.suffix}")
                src.replace(dst)
                moved.append(member)
            self.processed_path(member).unlink(missing_ok=True)

        del self.photos[name]
        self.order = [n for n in self.order if n != name]
        self.save()
        return moved

    def sort_by_date(self) -> None:
        """Oldest first, so the queue drains in the order the photos happened.

        Undated photos keep to the back rather than being guessed at.
        """
        self.order.sort(key=lambda n: (self.photos[n].date is None, self.photos[n].date or "", n))
        self.save()

    def shuffle(self) -> None:
        """Deal the queue into a random order.

        Only the unpublished part moves. Posted entries keep their exact index —
        they're the record of what already went out, and shuffling history would
        be a lie about the profile.
        """
        movable = [n for n in self.order if self.photos[n].status != STATUS_POSTED]
        random.shuffle(movable)
        dealt = iter(movable)
        self.order = [
            n if self.photos[n].status == STATUS_POSTED else next(dealt) for n in self.order
        ]
        self.save()

    def promote(self, lead: str, member: str) -> list[tuple[str, str]]:
        """Make `member` the first photo of the carousel led by `lead`.

        The A/B/C letters are the grouping rule, so they're also the fix: the
        photos are re-lettered on disk into the order asked for, the rest keeping
        their relative places. Renaming rather than storing an override means the
        filenames never disagree with the app, and `rescan` — which rebuilds
        every carousel from the folder — can't undo it on the next refresh.

        Returns the (old, new) renames. Raises KeyError if `member` isn't in
        this post.
        """
        photo = self.photos[lead]
        files = photo.files
        if member not in files:
            raise KeyError(member)
        if member == files[0]:
            return []

        base = CAROUSEL_SUFFIX.fullmatch(Path(lead).stem)["base"]
        wanted = [member, *(f for f in files if f != member)]
        renames = [
            (name, f"{base}{CAROUSEL_LETTERS[i]}{Path(name).suffix}")
            for i, name in enumerate(wanted)
        ]
        renames = [(old, new) for old, new in renames if old != new]

        # Two passes, via dotted temporaries: the names are being permuted, so a
        # direct rename would land on a file that hasn't moved out of the way
        # yet. A dot prefix also keeps a half-finished swap out of `rescan`.
        for resolve in (self.raw_path, self.processed_path):
            for old, new in renames:
                src = resolve(old)
                if src.is_file():
                    src.replace(src.with_name(f".promote-{resolve(new).name}"))
            for _, new in renames:
                dst = resolve(new)
                tmp = dst.with_name(f".promote-{dst.name}")
                if tmp.is_file():
                    tmp.replace(dst)

        self.rescan()
        self.save()
        return renames

    def known_file(self, name: str) -> bool:
        """True for any raw file the app is willing to serve, carousels included."""
        return any(name in photo.files for photo in self.photos.values())

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
