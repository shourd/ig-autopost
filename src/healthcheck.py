"""Weekly proof that the pipeline can still do its job — and a nudge if it can't.

    uv run python -m src.healthcheck

This exists because of how this project fails: silently. A Meta token was
invalidated on about 22 September 2026 and nothing said so; it was found two
weeks and six missed slots later by trying to post. Nothing was broken loudly,
so nothing got fixed.

Two jobs, and the second is the one that actually keeps the posting going:

  1. Check the things that rot — the access token, the image URLs Meta has to
     fetch, whether there's anything left in the queue.
  2. Rewrite the reminders. The reminder window is only the next few posts, and
     it was only ever refilled by pressing Save in the app. Stop pressing Save
     and the nudges quietly run out, which is exactly what happened.

A failure is reported into the Reminders app, due immediately, because that is
the channel already proven to reach the phone. Printing to a log file nobody
opens would repeat the original mistake.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests

from src.config import REPO_ROOT, load_config, secret

# Meta's long-lived tokens last 60 days. Ten days of warning is two or three
# posting slots — enough to re-authorise without a gap, even on holiday.
TOKEN_LIFETIME = timedelta(days=60)
WARN_WITHIN = timedelta(days=10)
TIMEOUT = 30


@dataclass
class Check:
    ok: bool
    label: str
    detail: str = ""
    fix: str = ""  # what to do about it — goes in the alert, so write it for a phone

    def line(self) -> str:
        return f"  {'ok ' if self.ok else '!  '}{self.label}{': ' + self.detail if self.detail else ''}"


def check_token() -> Check:
    """Ask the API, rather than trusting that a token exists.

    Presence proves nothing: the token that caused the outage was sitting in
    .env the whole time, the right length, and completely dead.
    """
    token = secret("META_ACCESS_TOKEN", required=False)
    ig_id = secret("IG_USER_ID", required=False)
    if not token or not ig_id:
        return Check(False, "Meta token", "META_ACCESS_TOKEN or IG_USER_ID is not set",
                     "Run: uv run python -m src.token_setup")

    kind = secret("META_TOKEN_KIND", required=False) or "instagram"
    host = "graph.instagram.com" if kind == "instagram" else "graph.facebook.com"
    try:
        resp = requests.get(f"https://{host}/v26.0/{ig_id}/media",
                            params={"fields": "id", "limit": 1, "access_token": token},
                            timeout=TIMEOUT)
    except requests.RequestException as exc:
        # No network is not a dead token; say so rather than crying wolf.
        return Check(False, "Meta token", f"could not reach {host} ({type(exc).__name__})",
                     "Probably just offline. If it persists, check the token.")

    if resp.status_code == 200:
        return Check(True, "Meta token", "valid, and the account id resolves")
    error = resp.json().get("error", {})
    return Check(False, "Meta token", error.get("message", f"HTTP {resp.status_code}"),
                 "The token is dead — re-authorise: uv run python -m src.token_setup")


def check_token_age(now: datetime | None = None) -> Check:
    """How much of the 60 days is left, counted from when it was issued.

    The API won't say: debug_token needs an app secret this setup doesn't have,
    so the issue date is written down by token_setup instead.
    """
    issued = secret("META_TOKEN_ISSUED", required=False)
    if not issued:
        return Check(True, "Token age", "unknown — recorded from the next token_setup on")

    now = now or datetime.now(timezone.utc)
    expires = datetime.fromisoformat(issued) + TOKEN_LIFETIME
    left = expires - now
    if left <= timedelta():
        return Check(False, "Token age", f"expired {-left.days}d ago",
                     "Re-authorise: uv run python -m src.token_setup")
    if left <= WARN_WITHIN:
        return Check(False, "Token age", f"expires in {left.days}d ({expires:%-d %b})",
                     f"Token expires {expires:%-d %b}. Re-authorise: "
                     "uv run python -m src.token_setup")
    return Check(True, "Token age", f"{left.days}d left (expires {expires:%-d %b})")


def check_queue(state) -> Check:
    """An empty queue stops the posting just as dead as a broken token."""
    from app.state import STATUS_READY

    ready = [p for p in state.ordered() if p.status == STATUS_READY]
    if not ready:
        return Check(False, "Queue", "nothing ready to post",
                     "The queue is empty — drop photos in photos/raw/ and Save.")
    weeks = len(ready) / max(len(state.cfg.schedule.slots), 1)
    if len(ready) <= len(state.cfg.schedule.slots):
        return Check(False, "Queue", f"only {len(ready)} post(s) left",
                     f"Only {len(ready)} post(s) left in the queue.")
    return Check(True, "Queue", f"{len(ready)} ready, about {weeks:.0f} weeks")


def check_image(state) -> Check:
    """Prove Meta could fetch the next photo, before the slot arrives.

    The image is served from the public repo, so a render that was committed but
    never pushed is a 404 at publish time and nowhere else.
    """
    from app.state import STATUS_READY
    from src.publish import PublishError, image_url, preflight

    nxt = next((p for p in state.ordered() if p.status == STATUS_READY), None)
    if nxt is None:
        return Check(True, "Image URL", "nothing queued to check")

    url = image_url(state.processed_path(nxt.file).name, state.cfg)
    try:
        preflight(url)
    except PublishError as exc:
        return Check(False, "Image URL", str(exc).splitlines()[0],
                     "Meta can't fetch the next photo. Press Save in the app to "
                     "commit and push the renders.")
    except requests.RequestException as exc:
        return Check(False, "Image URL", f"unreachable ({type(exc).__name__})",
                     "Probably just offline.")
    return Check(True, "Image URL", f"{nxt.file} is reachable")


def refresh_reminders(state) -> list[Check]:
    """Rewrite the nudges. This is the half that keeps the posting happening.

    `sync` marks its own failures with a leading "!", and reports both channels
    separately — Todoist can be down while the Reminders alarm is fine.
    """
    from src import reminders
    from src.publish import image_url

    try:
        log = reminders.sync(state, image_url)
    except Exception as exc:  # a reminder problem must not take the check down
        return [Check(False, "Reminders", f"{type(exc).__name__}: {exc}",
                      "The reminder sync failed; run: uv run python -m src.reminders")]

    return [
        Check(not line.startswith("!"), "Reminders", line.lstrip("! ").strip(),
              "Reminders aren't being written — run: uv run python -m src.reminders")
        for line in log
    ] or [Check(True, "Reminders", "switched off in config.yaml")]


def run(state=None) -> list[Check]:
    from app.state import State

    state = state or State()
    checks = [check_token(), check_token_age(), check_queue(state), check_image(state)]
    # Deliberately last: the reminders are rewritten even when something above
    # has failed, because a dead token is no reason to also stop nudging.
    return checks + refresh_reminders(state)


# --- the weekly schedule ---------------------------------------------------

LABEL = "com.snapposter.healthcheck"
# Monday morning: two clear days before the Wednesday slot, so a dead token is
# found with time to fix it rather than at the moment it's needed.
WEEKDAY, HOUR, MINUTE = 1, 9, 0

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{uv}</string>
    <string>run</string>
    <string>python</string>
    <string>-m</string>
    <string>src.healthcheck</string>
  </array>
  <key>WorkingDirectory</key><string>{repo}</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>{weekday}</integer>
    <key>Hour</key><integer>{hour}</integer>
    <key>Minute</key><integer>{minute}</integer>
  </dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def _plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def _log_path() -> Path:
    return Path.home() / "Library/Logs/ig-autopost-healthcheck.log"


def install() -> None:
    """Write the LaunchAgent and start it, then run it once.

    launchd rather than cron or a GitHub Action: the reminders live in the
    Reminders app on this Mac, so no runner anywhere else can write them. A
    missed run — the laptop shut, say — fires when the machine next wakes.
    """
    import os
    import shutil
    import subprocess

    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    plist, log = _plist_path(), _log_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(PLIST.format(
        label=LABEL, uv=uv, repo=REPO_ROOT, log=log,
        weekday=WEEKDAY, hour=HOUR, minute=MINUTE,
    ))

    target = f"gui/{os.getuid()}"
    # bootout first so re-installing picks up an edited plist instead of being
    # refused as already loaded.
    subprocess.run(["launchctl", "bootout", f"{target}/{LABEL}"], capture_output=True)
    done = subprocess.run(["launchctl", "bootstrap", target, str(plist)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"  launchctl refused the job:\n    {done.stderr.strip()}")

    print(f"\n  Installed {LABEL}")
    print(f"    runs      Mondays at {HOUR:02d}:{MINUTE:02d}")
    print(f"    plist     {plist}")
    print(f"    log       {log}")
    print("\n  Running it once now, so macOS asks for Reminders permission "
          "while you're here…")
    subprocess.run(["launchctl", "kickstart", f"{target}/{LABEL}"], capture_output=True)


def uninstall() -> None:
    import os
    import subprocess

    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    _plist_path().unlink(missing_ok=True)
    print(f"  Removed {LABEL}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Weekly health check.")
    parser.add_argument("--install", action="store_true", help="schedule it weekly (launchd)")
    parser.add_argument("--uninstall", action="store_true", help="remove the weekly schedule")
    args = parser.parse_args()
    if args.install:
        return install()
    if args.uninstall:
        return uninstall()

    cfg = load_config()
    checks = run()
    stamp = datetime.now().strftime("%a %-d %b %Y, %H:%M")
    print(f"\n  ig-autopost health check — {stamp}")
    for check in checks:
        print(check.line())

    failed = [c for c in checks if not c.ok]
    from src.apple_reminders import alert

    body = None
    if failed:
        body = "\n".join(
            [f"{c.label}: {c.detail}" for c in failed]
            + ["", *dict.fromkeys(c.fix for c in failed if c.fix)]
        )
    # Always called: with nothing wrong this clears last week's alert, so a
    # stale "needs attention" can't sit on the phone after the fix.
    for line in alert(body, cfg.publish.reminder_apple_list):
        print(f"  {line}")

    print(f"\n  {len(checks) - len(failed)}/{len(checks)} ok\n")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
