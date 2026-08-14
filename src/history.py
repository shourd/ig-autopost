"""Backfill the real profile grid from the Instagram API.

    uv run python -m src.history

Downloads every published post's cover image into photos/history/ so the
curation app can show the queue sitting on top of the actual profile, rather
than against an empty grid.

Display-only by design. The publisher never reads this folder — it writes to
photos/posted/ — so these files are git-ignored. They are already public on
Instagram; committing ~100 copies would add megabytes to the repo to duplicate
what Instagram already serves.

Re-running is cheap and safe: existing files are skipped, so this doubles as
"fetch whatever I've posted since last time".
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import requests
import yaml

from src.config import load_config, secret

GRAPH = "https://graph.instagram.com/v26.0"
TIMEOUT = 60
PAGE_SIZE = 100  # Meta's maximum; ~100 posts is one round trip.

# CAROUSEL_ALBUM exposes the first slide as media_url, which is what the grid
# shows. VIDEO/REELS have no media_url worth showing, so they fall back to the
# poster frame — the grid is about colour and composition, not playback.
COVER_FIELD = {"IMAGE": "media_url", "CAROUSEL_ALBUM": "media_url"}
FIELDS = "id,media_type,media_url,thumbnail_url,permalink,caption,timestamp"
PROFILE_FIELDS = (
    "username,name,biography,followers_count,follows_count,"
    "media_count,profile_picture_url"
)


def _shortcode(permalink: str) -> str:
    """The /p/<code>/ segment — stable, unlike the numeric media id."""
    match = re.search(r"/(?:p|reel)/([^/]+)", permalink or "")
    return match.group(1) if match else "unknown"


def fetch_profile(token: str, folder: Path) -> dict:
    """Read the live profile and cache it for the app's mock header.

    Worth fetching rather than hand-copying into config.yaml: a header typed
    from a screenshot silently describes whichever account was screenshotted,
    which is exactly how this project spent a week showing the wrong one.
    """
    resp = requests.get(
        f"{GRAPH}/me", params={"fields": PROFILE_FIELDS, "access_token": token}, timeout=TIMEOUT
    )
    payload = resp.json()
    if "error" in payload:
        print(f"  ! profile fetch failed: {payload['error'].get('message')}")
        return {}

    folder.mkdir(parents=True, exist_ok=True)
    if avatar_url := payload.pop("profile_picture_url", None):
        try:
            got = requests.get(avatar_url, timeout=TIMEOUT)
            got.raise_for_status()
            (folder / "avatar.jpg").write_bytes(got.content)
            payload["avatar"] = "photos/history/avatar.jpg"
        except requests.RequestException as exc:
            print(f"  ! avatar download failed: {exc}")

    (folder / "profile.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    )
    return payload


def fetch_media(token: str, ig_id: str) -> list[dict]:
    """Every post, newest first, following Meta's cursor pagination."""
    url = f"{GRAPH}/{ig_id}/media"
    params = {"fields": FIELDS, "limit": PAGE_SIZE, "access_token": token}
    out: list[dict] = []

    while url:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
        payload = resp.json()
        if "error" in payload:
            err = payload["error"]
            raise SystemExit(
                f"\n  Instagram refused the media listing:\n"
                f"    {err.get('message')}\n"
                f"    type={err.get('type')} code={err.get('code')}\n"
            )
        out += payload.get("data", [])
        # The `next` URL already carries fields/limit/token; re-sending params
        # alongside it would duplicate them.
        url, params = payload.get("paging", {}).get("next"), None
        print(f"  fetched {len(out)} post(s)…")

    return out


def download(media: list[dict], folder: Path) -> tuple[int, int]:
    """Save each cover image. Returns (downloaded, skipped)."""
    folder.mkdir(parents=True, exist_ok=True)
    got = skipped = 0

    for item in media:
        when = datetime.strptime(item["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        # Date-prefixed so the folder reads chronologically in Finder; the
        # shortcode keeps it unique and traceable back to the live post.
        dst = folder / f"{when:%Y-%m-%d}_{_shortcode(item['permalink'])}.jpg"
        if dst.exists():
            skipped += 1
            continue

        src_url = item.get(COVER_FIELD.get(item["media_type"], "thumbnail_url"))
        if not src_url:
            print(f"  ! no image for {item['permalink']} ({item['media_type']})")
            continue

        resp = requests.get(src_url, timeout=TIMEOUT)
        resp.raise_for_status()
        dst.write_bytes(resp.content)

        # The app orders the grid by mtime, so stamping the real post time is
        # what makes the backfilled grid match Instagram's own ordering.
        os.utime(dst, (when.timestamp(), when.timestamp()))
        got += 1

    return got, skipped


def main() -> None:
    cfg = load_config()
    token, ig_id = secret("META_ACCESS_TOKEN"), secret("IG_USER_ID")

    print("\n  Fetching your Instagram history…")
    profile = fetch_profile(token, cfg.paths.history)
    if profile:
        print(
            f"  @{profile.get('username')} — {profile.get('media_count')} posts, "
            f"{profile.get('followers_count')} followers"
        )

    media = fetch_media(token, ig_id)
    if not media:
        raise SystemExit("  No posts came back — nothing to backfill.\n")

    got, skipped = download(media, cfg.paths.history)

    # A manifest so captions and permalinks survive without re-hitting the API,
    # and so a later feature can diff "what's live" against "what we published".
    manifest = [
        {
            "file": f"{datetime.strptime(m['timestamp'], '%Y-%m-%dT%H:%M:%S%z'):%Y-%m-%d}"
            f"_{_shortcode(m['permalink'])}.jpg",
            "id": m["id"],
            "permalink": m["permalink"],
            "caption": m.get("caption") or "",
            "timestamp": m["timestamp"],
            "media_type": m["media_type"],
        }
        for m in media
    ]
    (cfg.paths.history / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True, width=1000)
    )

    newest = media[0]["timestamp"][:10]
    print(
        f"\n  {len(media)} post(s) on the account.\n"
        f"  {got} image(s) downloaded, {skipped} already had.\n"
        f"  Most recent: {newest}\n"
        f"  -> {cfg.paths.history.relative_to(cfg.paths.history.parent.parent)}\n"
        f"\n  Run `uv run app` to see the queue on top of the real grid.\n"
    )


if __name__ == "__main__":
    main()
