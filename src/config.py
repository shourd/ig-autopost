"""Configuration loading for ig-autopost.

Everything tunable lives in config.yaml; secrets live in .env (local) or the
environment (CI). Nothing in src/ carries a hardcoded canvas size or margin.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"


@dataclass(frozen=True)
class BorderConfig:
    canvas_w: int
    canvas_h: int
    min_margin_px: int
    background: str
    jpeg_quality: int
    max_bytes: int

    @property
    def fit_w(self) -> int:
        """Width of the box the photo must fit inside."""
        return self.canvas_w - 2 * self.min_margin_px

    @property
    def fit_h(self) -> int:
        return self.canvas_h - 2 * self.min_margin_px


@dataclass(frozen=True)
class CaptionConfig:
    model: str
    max_tokens: int
    effort: str
    max_chars: int
    enabled: bool = False


@dataclass(frozen=True)
class ProfileConfig:
    """Cosmetic values for the mock Instagram header in the curation app."""

    username: str
    display_name: str
    bio: str
    posts: int | None
    followers: int
    following: int
    avatar: str | None
    highlights: list[str]


@dataclass(frozen=True)
class Slot:
    """One recurring weekly posting time, in local wall-clock terms."""

    weekday: int  # 0 = Monday
    hour: int
    minute: int = 0


@dataclass(frozen=True)
class ScheduleConfig:
    """When posts go out.

    Times are local wall-clock in `timezone`, not UTC, because the whole point
    is to land inside a human attention window — and that window doesn't move
    twice a year when the clocks do. Conversion to UTC happens at read time.
    """

    timezone: str
    jitter_minutes: int
    slots: tuple[Slot, ...]

    @classmethod
    def from_raw(cls, raw: dict) -> "ScheduleConfig":
        slots = tuple(Slot(**s) for s in raw["slots"])
        if not slots:
            raise ValueError("config.yaml: schedule.slots must list at least one slot")
        return cls(
            timezone=raw["timezone"],
            jitter_minutes=int(raw.get("jitter_minutes", 0)),
            slots=tuple(sorted(slots, key=lambda s: (s.weekday, s.hour, s.minute))),
        )


@dataclass(frozen=True)
class PublishConfig:
    api_version: str
    branch: str
    poll_attempts: int
    poll_seconds: int
    todoist: bool
    reminders: bool = False
    reminder_count: int = 4
    reminder_lead_minutes: int = 120


@dataclass(frozen=True)
class Paths:
    raw: Path
    processed: Path
    posted: Path
    queue: Path
    history: Path


@dataclass(frozen=True)
class Config:
    border: BorderConfig
    caption: CaptionConfig
    profile: ProfileConfig
    schedule: ScheduleConfig
    publish: PublishConfig
    paths: Paths


def _load_raw(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    with open(path) as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=None)
def load_config(path: Path | None = None) -> Config:
    raw = _load_raw(path)
    p = raw["paths"]
    return Config(
        border=BorderConfig(**raw["border"]),
        caption=CaptionConfig(**raw["caption"]),
        profile=ProfileConfig(**raw["profile"]),
        schedule=ScheduleConfig.from_raw(raw["schedule"]),
        publish=PublishConfig(**raw["publish"]),
        paths=Paths(
            raw=REPO_ROOT / p["raw"],
            processed=REPO_ROOT / p["processed"],
            posted=REPO_ROOT / p["posted"],
            queue=REPO_ROOT / p["queue"],
            history=REPO_ROOT / p["history"],
        ),
    )


def secret(name: str, required: bool = True) -> str | None:
    """Read a secret from the environment, loading .env first if present.

    CI sets these directly in the environment; locally they come from .env.

    .env wins over a blank environment variable, which is the whole point: the
    template ships every name with an empty value, so a long-running app that
    read .env before a key was filled in would otherwise cache the blank forever
    and keep reporting the secret as missing after it had been set.
    """
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        from dotenv import dotenv_values, load_dotenv

        load_dotenv(env_file)
        if not os.environ.get(name):
            os.environ.pop(name, None)
            from_file = dotenv_values(env_file).get(name)
            if from_file:
                os.environ[name] = from_file
    value = os.environ.get(name)
    if required and not value:
        raise RuntimeError(
            f"Missing required secret {name!r}. "
            f"Set it in {env_file} locally, or as a GitHub Actions secret in CI."
        )
    return value
