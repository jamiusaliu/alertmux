"""`alertmux-notify` -- the notifier's command-line entry point.

One invocation is one poll cycle: load config, collect from every default
adapter (via `alertmux.query.collect`, the same aggregation the HTTP API
uses), run the notifier pipeline, and exit. Scheduling repeated polls is
the operator's job (cron, systemd timer) -- see the README's
"Notifications" section for a worked example.
"""

from __future__ import annotations

import argparse
import logging
import sys

from alertmux.adapters import default_adapters
from alertmux.notify.config import ConfigError, load_config
from alertmux.notify.delivery import SmtpSender
from alertmux.notify.runner import run_once
from alertmux.notify.state import StateStore
from alertmux.query import collect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alertmux-notify",
        description="Poll alertmux's sources and email matching alerts over your own SMTP.",
    )
    parser.add_argument(
        "--config", required=True, help="Path to the notifier's TOML config file."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent without sending anything or writing state.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable INFO-level logging (default WARNING)."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("alertmux.notify.cli")

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # ConfigError's message is already scrubbed of credentials
        # (config.py). Safe to print in full.
        print(f"alertmux-notify: {exc}", file=sys.stderr)
        return 2

    rules = config.rule_configs()
    if not rules:
        print("alertmux-notify: config has no [[rules]] -- nothing to do", file=sys.stderr)
        return 2

    state = StateStore(config.state.path)
    state.load()
    pruned = state.prune(config.state.prune_after_days)
    if pruned:
        logger.info("pruned %d state entries older than %d days", pruned, config.state.prune_after_days)

    response = collect(default_adapters())
    if response.partial:
        logger.warning(
            "this fetch is partial (a source failed, was truncated, or quarantined "
            "records) -- matching and notifications proceed on the data that did "
            "arrive, but coverage this run may be incomplete"
        )

    sender = None if args.dry_run else SmtpSender(config.smtp)
    if not args.dry_run:
        logger.info("SMTP: %s", config.smtp.safe_summary())

    report = run_once(
        response,
        rules,
        state,
        smtp_config=config.smtp,
        sender=sender,
        dry_run=args.dry_run,
    )

    for warning in report.warnings:
        print(f"WARNING: {warning}")

    # Printed in both dry-run and real runs, and before the send/would-send
    # summary below, since a suppressed count is the one thing an operator
    # must never miss -- a silently truncated hazard list is exactly the
    # failure mode this project exists to prevent. `report.dry_run_would_send`
    # already reflects the cap (runner.py applies it before dry-run decides
    # what to report), so without this line first, a dry run's "N alert(s)
    # would be sent" reads as complete when it is not.
    for rule_name, suppressed in report.suppressed_by_rate_limit.items():
        print(
            f"CAPPED: rule '{rule_name}' suppressed {suppressed} alert(s) this "
            f"run by the per-run ceiling (will retry next run, not recorded as "
            f"notified). Consider digest = true or a narrower rule."
        )

    if args.dry_run:
        if report.dry_run_would_send:
            print(f"Dry run: {len(report.dry_run_would_send)} alert(s) would be sent:")
            for line in report.dry_run_would_send:
                print(f"  {line}")
        else:
            print("Dry run: nothing would be sent.")
    else:
        print(f"Sent {report.sent_count} message(s).")

    if not args.dry_run:
        if report.failures:
            print(f"{len(report.failures)} delivery failure(s):", file=sys.stderr)
            for failure in report.failures:
                print(f"  {failure}", file=sys.stderr)

    if not report.ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
