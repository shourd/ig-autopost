"""Local curation app.

Python stdlib only on the server side — no web framework. The API surface is
about eight endpoints, which a hand-rolled router handles without dragging in a
dependency the GitHub Actions runner would also have to install.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from app.state import STATUS_HOLD, STATUS_POSTED, STATUS_READY, State
from src.border import add_border
from src.caption import draft_caption
from src.config import REPO_ROOT, secret

STATIC = Path(__file__).parent / "static"
MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}

_state = State()
_lock = threading.Lock()


def _ensure_processed(name: str) -> Path:
    """Render the bordered JPEG if it's missing or stale, and return its path.

    Done lazily on image request rather than up front, so the grid paints
    immediately on first load instead of blocking on a full pass.
    """
    src, dst = _state.raw_path(name), _state.processed_path(name)
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    result = add_border(src, dst, _state.cfg.border)
    with _lock:
        photo = _state.photos.get(name)
        if photo:
            photo.upscale_blocked = result.upscale_blocked
            _refresh_flags(photo)
            _state.save()
    return dst


def _refresh_flags(photo) -> None:
    """Recompute the warning badges from current state.

    Rebuilt from scratch every time rather than accumulated, so flags left over
    from a previous configuration can't linger. `no_place` and `caption_failed`
    only mean anything while auto-drafting is on; with captions written by hand
    they'd be permanent false alarms.
    """
    from src.caption import FLAG_CAPTION_FAILED, FLAG_NO_DATE, FLAG_NO_PLACE
    from src.exif import read_capture_date

    drafting = _state.cfg.caption.enabled
    flags = [f for f in photo.flags if f == FLAG_CAPTION_FAILED and drafting]

    when, _ = read_capture_date(_state.raw_path(photo.file))
    photo.date = when.isoformat() if when else None
    if when is None:
        flags.append(FLAG_NO_DATE)
    if drafting and not photo.place:
        flags.append(FLAG_NO_PLACE)
    if photo.upscale_blocked:
        flags.append("too_small")
    photo.flags = flags


def _draft(names: list[str]) -> list[str]:
    """Draft captions, returning one message per photo that failed.

    DISABLED: `caption.enabled` is false in config.yaml — Sjoerd writes captions
    by hand. The code and its tests are left intact; flipping that flag back to
    true re-arms drafting everywhere (this function, Save, and the API route).

    A drafting failure — no API key, a network blip — must not take down the
    whole Save. The photo gets flagged, the reason is reported, and the queue
    still gets written.
    """
    from src.caption import FLAG_CAPTION_FAILED

    if not _state.cfg.caption.enabled:
        return ["caption drafting is disabled (config.yaml: caption.enabled)"]

    errors: list[str] = []
    for name in names:
        photo = _state.photos.get(name)
        if not photo or photo.caption_reviewed:
            continue
        _ensure_processed(name)
        try:
            draft = draft_caption(
                _state.processed_path(name),
                place=photo.place,
                source_for_date=_state.raw_path(name),
                cfg=_state.cfg.caption,
            )
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            with _lock:
                photo.flags = sorted(set(photo.flags) | {FLAG_CAPTION_FAILED})
                _state.save()
            continue
        with _lock:
            photo.caption = draft.caption
            photo.drafted = True
            photo.date = draft.date.isoformat() if draft.date else None
            extra = {"too_small"} if photo.upscale_blocked else set()
            photo.flags = sorted(set(draft.flags) | extra)
            _state.save()
    return errors


def _git(*args: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _save() -> dict:
    """Render, draft anything missing, write queue.yaml, commit and push."""
    log: list[str] = []
    publishable = [p for p in _state.ordered() if p.status != STATUS_POSTED]

    for photo in publishable:
        _ensure_processed(photo.file)
    log.append(f"rendered {len(publishable)} photo(s)")

    if _state.cfg.caption.enabled:
        missing = [p.file for p in publishable if not p.caption and not p.drafted]
        if missing:
            errors = _draft(missing)
            log.append(f"drafted {len(missing) - len(errors)}/{len(missing)} caption(s)")
            log += [f"  ! {e}" for e in errors]
    else:
        blank = sum(1 for p in publishable if not p.caption.strip())
        if blank:
            log.append(f"{blank} photo(s) still have no caption")

    entries = [p.queue_entry() for p in _state.ordered()]
    _state.cfg.paths.queue.write_text(
        yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, width=1000)
    )
    log.append(f"wrote queue.yaml ({len(entries)} entries)")
    _state.save()

    # Exactly what the Actions runner reads at publish time. photos/raw stays
    # local; ignoring photos/ wholesale is what breaks publishing with a 404.
    ok, out = _git("add", "photos/processed", "photos/posted", "queue.yaml", "config.yaml")
    if not ok:
        return {"ok": False, "log": log, "error": f"git add failed: {out}"}

    ok, out = _git("diff", "--cached", "--quiet")
    if ok:  # exit 0 from --quiet means no staged changes
        log.append("nothing to commit")
        return {"ok": True, "log": log}

    ok, out = _git("commit", "-m", f"Queue {len(entries)} post(s)")
    if not ok:
        return {"ok": False, "log": log, "error": f"git commit failed: {out}"}
    log.append("committed")

    ok, out = _git("push")
    if not ok:
        # A missing remote shouldn't look like a failed save — the commit landed.
        log.append(f"push skipped: {out.splitlines()[-1] if out else 'no remote'}")
        return {"ok": True, "log": log, "warning": "committed locally but not pushed"}
    log.append("pushed")
    return {"ok": True, "log": log}


def _post_now(confirm: bool) -> dict:
    """Publish the next queued photo immediately, ahead of its schedule.

    Defaults to a dry run: the button checks that Meta could fetch the image
    and reports what it would post, and only a second explicit click publishes.
    Publishing is irreversible from here — the API has no unpublish.
    """
    from src.publish import PublishError, publish_next

    missing = [
        name for name in ("IG_USER_ID", "META_ACCESS_TOKEN")
        if not secret(name, required=False)
    ]
    if missing:
        return {
            "ok": False,
            "reason": f"Not set yet: {', '.join(missing)}. See README → Enabling auto-posting.",
        }

    try:
        with _lock:
            return publish_next(_state, confirm=confirm)
    except PublishError as exc:
        return {"ok": False, "reason": str(exc)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter console
        pass

    # --- plumbing ----------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    # --- routes ------------------------------------------------------------

    def do_GET(self) -> None:
        try:
            path = self.path.split("?")[0]
            if path == "/":
                return self._file(STATIC / "index.html")
            if path.startswith("/static/"):
                return self._file(STATIC / Path(path).name)
            if path.startswith("/img/"):
                name = Path(path[len("/img/") :]).name
                if name not in _state.photos:
                    return self._json({"error": "unknown photo"}, 404)
                dst = _ensure_processed(name)
                return self._send(200, dst.read_bytes(), "image/jpeg")
            if path.startswith("/posted/"):
                name = Path(path[len("/posted/") :]).name
                src = _state.posted_path(name)
                if src is None:
                    return self._json({"error": "unknown photo"}, 404)
                return self._send(200, src.read_bytes(), "image/jpeg")
            if path == "/avatar":
                src = _state.avatar_path()
                if src is None:
                    return self._json({"error": "no avatar configured"}, 404)
                return self._send(200, src.read_bytes(), "image/jpeg")
            if path == "/api/photos":
                with _lock:
                    _state.rescan()
                    for photo in _state.photos.values():
                        _refresh_flags(photo)
                    _state.save()
                    return self._json(_state.as_json())
            self._json({"error": "not found"}, 404)
        except Exception:
            self._json({"error": traceback.format_exc()}, 500)

    def do_POST(self) -> None:
        try:
            path = self.path.split("?")[0]
            body = self._body()

            if path == "/api/order":
                with _lock:
                    known = set(_state.photos)
                    _state.order = [n for n in body["order"] if n in known]
                    _state.order += [n for n in known if n not in _state.order]
                    _state.save()
                return self._json({"ok": True})

            if path == "/api/place":
                with _lock:
                    place = (body.get("place") or "").strip() or None
                    for name in body["files"]:
                        if name in _state.photos:
                            _state.photos[name].place = place
                            _refresh_flags(_state.photos[name])
                    _state.save()
                return self._json(_state.as_json())

            if match := re.fullmatch(r"/api/photo/(.+)", path):
                name = Path(match.group(1)).name
                with _lock:
                    photo = _state.photos.get(name)
                    if not photo:
                        return self._json({"error": "unknown photo"}, 404)
                    if "caption" in body:
                        photo.caption = body["caption"]
                        # Any keystroke means Sjoerd has looked at it.
                        photo.caption_reviewed = True
                    if "status" in body:
                        photo.status = body["status"]
                    _refresh_flags(photo)
                    _state.save()
                    return self._json({"photo": _state.as_json()["photos"][name]})

            # Auto-drafting is off (config.yaml: caption.enabled). The route is
            # left wired so re-enabling is a one-line config change.
            if path == "/api/draft":
                names = body.get("files") or [
                    p.file for p in _state.ordered()
                    if not p.caption and p.status != STATUS_POSTED
                ]
                errors = _draft(names)
                return self._json({**_state.as_json(), "errors": errors})

            if path == "/api/post-now":
                return self._json(_post_now(confirm=bool(body.get("confirm"))))

            if path == "/api/save":
                return self._json(_save())

            self._json({"error": "not found"}, 404)
        except Exception:
            self._json({"error": traceback.format_exc()}, 500)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            return self._json({"error": "not found"}, 404)
        self._send(200, path.read_bytes(), MIME.get(path.suffix, "text/plain"))


def serve(port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"  curation app -> http://127.0.0.1:{port}")
    print(f"  {len(_state.photos)} photo(s) in {_state.cfg.paths.raw.relative_to(REPO_ROOT)}")
    server.serve_forever()
