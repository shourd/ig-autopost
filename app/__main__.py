"""Entry point for `uv run app`."""

from __future__ import annotations

import argparse
import threading
import webbrowser


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Instagram curation app")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = parser.parse_args()

    from app.server import serve

    if not args.no_open:
        threading.Timer(
            0.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")
        ).start()
    try:
        serve(args.port)
    except KeyboardInterrupt:
        print("\n  stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
