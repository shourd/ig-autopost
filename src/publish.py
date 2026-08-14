"""Publish the next queued photo to Instagram.

    uv run python -m src.publish              # dry run: checks everything, posts nothing
    uv run python -m src.publish --confirm    # actually publishes

Manual only for now. There is deliberately no GitHub Actions workflow yet —
the schedule gets wired up once a real post has gone out by hand.

Instagram publishing is a three-step dance, not one upload:

    POST /<IG_ID>/media          -> a container id (Meta fetches the image itself)
    GET  /<container>?status_code -> poll until FINISHED
    POST /<IG_ID>/media_publish  -> the live post

A carousel is the same dance with an extra lap: one container per photo marked
`is_carousel_item`, each polled to FINISHED, then a parent container with
`media_type=CAROUSEL` that carries the caption and lists the children.

Meta pulls the JPEG from a public HTTPS URL rather than accepting an upload,
which is why the file has to be committed and pushed before this runs. The
pre-flight check below is the difference between a clear error here and an
opaque container failure from Meta.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from app.state import STATUS_POSTED, STATUS_READY, State
from src.config import REPO_ROOT, load_config, secret

TIMEOUT = 60
TODOIST_API = "https://api.todoist.com/api/v1/tasks"


class PublishError(RuntimeError):
    """Anything that should stop the run and, in CI, open an issue."""


# --- where Meta fetches the image from ------------------------------------


def _origin_slug() -> str:
    """`owner/repo` from the git remote, for building raw.githubusercontent URLs."""
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise PublishError("No git remote 'origin'. Meta needs a public URL to fetch from.")
    # Handles both git@github.com:owner/repo.git and https://github.com/owner/repo.git
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+)", proc.stdout.strip())
    if not match:
        raise PublishError(f"Can't parse a GitHub owner/repo from {proc.stdout.strip()!r}")
    return match.group(1)


def image_url(filename: str, cfg) -> str:
    return (
        f"https://raw.githubusercontent.com/{_origin_slug()}"
        f"/{cfg.publish.branch}/photos/processed/{filename}"
    )


def preflight(url: str) -> None:
    """Prove Meta will be able to fetch this before asking it to.

    A container created against an unreachable URL fails asynchronously with a
    generic error, so catching it here is worth the extra request.
    """
    resp = requests.get(url, timeout=TIMEOUT, stream=True)
    if resp.status_code != 200:
        raise PublishError(
            f"Image URL returns {resp.status_code}, so Meta can't fetch it:\n    {url}\n"
            "  The processed JPEG must be committed AND pushed, and the repo public."
        )
    ctype = resp.headers.get("content-type", "")
    if ctype != "image/jpeg":
        raise PublishError(f"Image URL serves {ctype!r}, but Meta accepts JPEG only:\n    {url}")
    resp.close()


# --- the Meta calls --------------------------------------------------------


def _graph(cfg) -> str:
    return f"https://graph.instagram.com/{cfg.publish.api_version}"


def _post(url: str, **data) -> dict:
    resp = requests.post(url, data=data, timeout=TIMEOUT)
    payload = resp.json()
    if "error" in payload:
        err = payload["error"]
        raise PublishError(
            f"{err.get('message')}\n"
            f"    type={err.get('type')} code={err.get('code')} "
            f"subcode={err.get('error_subcode')}\n"
            f"    full body: {payload}"
        )
    return payload


def create_container(photo, urls: list[str], token: str, ig_id: str, cfg) -> str:
    """Stage the post and return its container id. Two shapes, one entry point.

    A single photo is one container. A carousel is one container per photo with
    `is_carousel_item`, each of which must finish ingesting on its own, and then
    a parent container that carries the caption and lists the children. Getting
    this wrong is silent: a caption on a child is simply dropped.
    """
    endpoint = f"{_graph(cfg)}/{ig_id}/media"
    params = {"caption": photo.caption, "access_token": token}
    # location_id is passed through from queue.yaml, never synthesised — and on
    # the Instagram Login path it is simply unavailable, so it stays absent.
    if photo.location_id:
        params["location_id"] = photo.location_id

    if len(urls) == 1:
        return _post(endpoint, image_url=urls[0], **params)["id"]

    children = []
    for index, url in enumerate(urls, start=1):
        child = _post(
            endpoint, image_url=url, is_carousel_item="true", access_token=token
        )["id"]
        print(f"        child {index}/{len(urls)}: {child}")
        # Children have to be FINISHED before the parent can reference them.
        await_ready(child, token, cfg)
        children.append(child)

    return _post(
        endpoint, media_type="CAROUSEL", children=",".join(children), **params
    )["id"]


def await_ready(container: str, token: str, cfg) -> None:
    """Poll until Meta has finished ingesting the image."""
    for attempt in range(cfg.publish.poll_attempts):
        resp = requests.get(
            f"{_graph(cfg)}/{container}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=TIMEOUT,
        ).json()
        status = resp.get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise PublishError(f"Meta rejected the container: {resp.get('status')}")
        if status == "EXPIRED":
            raise PublishError("Container expired before publishing (24h limit).")
        time.sleep(cfg.publish.poll_seconds)
        print(f"    still {status or 'IN_PROGRESS'} ({attempt + 1})…")
    raise PublishError(
        f"Container never reached FINISHED after "
        f"{cfg.publish.poll_attempts * cfg.publish.poll_seconds}s."
    )


def todoist_task(permalink: str, photo, cfg) -> None:
    """Log the live post. Never allowed to fail the publish — the post is out."""
    if not cfg.publish.todoist:
        return
    token = secret("TODOIST_API_TOKEN", required=False)
    if not token:
        print("  - Todoist skipped (no TODOIST_API_TOKEN)")
        return
    note = "" if photo.caption_reviewed else "\n\nNOTE: caption was never reviewed by hand."
    try:
        resp = requests.post(
            TODOIST_API,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": f"Instagram post live: {photo.file}",
                "description": f"{permalink}\n\n{photo.caption}{note}",
                "due_string": "today",
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        print("  - Todoist task created")
    except requests.RequestException as exc:
        print(f"  ! Todoist failed (post is still live, ignoring): {exc}")


# --- the run ---------------------------------------------------------------


def publish_next(
    state: State | None = None, confirm: bool = False, file: str | None = None
) -> dict:
    cfg = load_config()
    state = state or State()
    token, ig_id = secret("META_ACCESS_TOKEN"), secret("IG_USER_ID")

    ready = [p for p in state.ordered() if p.status == STATUS_READY]
    if file:
        photo = next((p for p in state.ordered() if p.file == file), None)
        if photo is None:
            return {"ok": False, "reason": f"{file} isn't in the queue."}
        if photo.status != STATUS_READY:
            return {"ok": False, "reason": f"{file} is {photo.status} — un-hold it first."}
    else:
        photo = next(iter(ready), None)
    if photo is None:
        return {"ok": False, "reason": "Queue is empty — nothing to publish."}

    urls = []
    for name in photo.files:
        processed = state.processed_path(name)
        if not processed.is_file():
            raise PublishError(f"{processed} doesn't exist. Run Save in the curation app first.")
        urls.append(image_url(processed.name, cfg))

    kind = f"carousel of {len(urls)}" if photo.is_carousel else "single photo"
    print(f"\n  Next up: {photo.file} ({kind})")
    print(f"  Caption: {photo.caption or '(empty)'}")
    for url in urls:
        print(f"  Image:   {url}")

    print("\n  [1/4] checking Meta can fetch the image…")
    for url in urls:
        preflight(url)
    print(f"        {len(urls)} reachable, image/jpeg")

    # An empty caption is legal at the API and almost never intended — captions
    # are hand-written now, so blank means "not written yet", not "no caption
    # wanted". Warned rather than blocked: it's a judgement call, not an error.
    warnings = []
    if not photo.caption.strip():
        warnings.append("This photo has NO CAPTION — it will post with an empty caption.")
    if not photo.caption_reviewed:
        warnings.append("Caption was never opened for review.")

    for line in warnings:
        print(f"\n  ! {line}")

    if not confirm:
        return {
            "ok": True,
            "dry_run": True,
            "file": photo.file,
            "files": photo.files,
            "caption": photo.caption,
            "url": urls[0],
            "warnings": warnings,
            "reason": "Dry run — everything checks out. Re-run with --confirm to publish.",
        }

    print("  [2/4] creating the media container…")
    container = create_container(photo, urls, token, ig_id, cfg)
    print(f"        container {container}")

    print("  [3/4] waiting for Meta to ingest…")
    await_ready(container, token, cfg)
    print("        FINISHED")

    print("  [4/4] publishing…")
    media_id = _post(
        f"{_graph(cfg)}/{ig_id}/media_publish", creation_id=container, access_token=token
    )["id"]
    permalink = requests.get(
        f"{_graph(cfg)}/{media_id}",
        params={"fields": "permalink", "access_token": token},
        timeout=TIMEOUT,
    ).json().get("permalink", "")
    print(f"        live: {permalink}")

    _mark_posted(state, photo, media_id, permalink, cfg)
    todoist_task(permalink, photo, cfg)

    return {
        "ok": True,
        "file": photo.file,
        "media_id": media_id,
        "permalink": permalink,
        "caption": photo.caption,
    }


def _mark_posted(state: State, photo, media_id: str, permalink: str, cfg) -> None:
    """Move the files, update the queue, commit. Never before the post is live."""
    for name in photo.files:
        src = state.processed_path(name)
        dst = cfg.paths.posted / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            src.replace(dst)

    photo.status = STATUS_POSTED
    state.save()

    entries = []
    for entry in yaml.safe_load(cfg.paths.queue.read_text()) or []:
        if entry.get("file") == photo.file:
            entry = {
                **entry,
                "status": STATUS_POSTED,
                "posted_at": datetime.now(timezone.utc).isoformat(),
                "media_id": media_id,
                "permalink": permalink,
            }
        entries.append(entry)
    cfg.paths.queue.write_text(
        yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, width=1000)
    )

    for args in (
        ["add", "photos/processed", "photos/posted", "queue.yaml"],
        ["commit", "-m", f"Published {photo.file}\n\n{permalink}"],
        ["push"],
    ):
        proc = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)
        if proc.returncode != 0:
            # The post is already live; a git problem must not look like a
            # publish failure, so it is reported and swallowed.
            print(f"  ! git {args[0]} failed (post is live): {proc.stderr.strip()}")
            return
    print("  - committed and pushed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm", action="store_true", help="actually publish (default is a dry run)"
    )
    parser.add_argument(
        "--file", help="publish this queued photo instead of the one at the front"
    )
    args = parser.parse_args()

    try:
        result = publish_next(confirm=args.confirm, file=args.file)
    except PublishError as exc:
        print(f"\n  FAILED: {exc}\n", file=sys.stderr)
        raise SystemExit(1)

    print()
    if result.get("dry_run"):
        print(f"  {result['reason']}\n")
    elif result.get("ok"):
        print(f"  Published {result['file']} -> {result['permalink']}\n")
    else:
        print(f"  {result['reason']}\n")


if __name__ == "__main__":
    main()
