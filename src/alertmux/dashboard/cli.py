"""`alertmux-dashboard` -- the dashboard's command-line entry point.

Starts the dashboard's own ASGI app (`dashboard.app:app`) via uvicorn.
Separate console script from `alertmux-mcp` and `alertmux-notify`, and
from anything in `api.py`, per the hard separation documented in
docs/DECISIONS.md.
"""

from __future__ import annotations

import argparse

import uvicorn

from alertmux.dashboard.app import app, configure
from alertmux.dashboard.volume import DEFAULT_VOLUME_PATH
from alertmux.notify.runlog import DEFAULT_RUN_LOG_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alertmux-dashboard",
        description=(
            "Run alertmux's local, read-only operational dashboard. "
            "Serves one self-contained HTML page plus its JSON endpoints."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8288, help="Bind port (default 8288).")
    parser.add_argument(
        "--volume-log",
        default=DEFAULT_VOLUME_PATH,
        help=f"Path to the volume-history JSONL file (default {DEFAULT_VOLUME_PATH!r}).",
    )
    parser.add_argument(
        "--run-log",
        default=DEFAULT_RUN_LOG_PATH,
        help=(
            "Path to the notifier run-log JSONL file (default "
            f"{DEFAULT_RUN_LOG_PATH!r}) -- point this at the same path "
            "your notify.toml's [run_log] uses so this dashboard shows "
            "that notifier's real outcomes."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure(volume_path=args.volume_log, run_log_path=args.run_log)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
