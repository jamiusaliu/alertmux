"""Ties config, rules, state and delivery together for one poll.

One call to `run_once` is one poll cycle: collect (done by the caller,
via `alertmux.query.collect`), filter out expired alerts, collapse
cross-source duplicates to the preferred record, evaluate every rule,
apply the per-rule rate ceiling, deliver (or, in dry-run, just report),
and persist state. Scheduling the next poll (cron, systemd timer, a
sleep loop) is left to the operator -- this module does exactly one
cycle and returns a report.

Failure is loud: an SMTP failure is logged at ERROR, recorded in the
returned report's `failures`, and deliberately **not** written to
state -- so the failed alert is retried on the next run instead of
being silently lost. `cli.py` turns a non-empty `failures` list into a
non-zero exit code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from alertmux.dedupe import DuplicateGroup
from alertmux.notify.config import SmtpConfig
from alertmux.notify.delivery import (
    DeliveryError,
    SmtpSender,
    build_alert_message,
    build_digest_message,
)
from alertmux.notify.rules import (
    RuleConfig,
    evaluate_rule,
    warn_severity_rules_against_unmapped_sources,
)
from alertmux.notify.state import StateStore
from alertmux.query import AlertsResponse
from alertmux.schema import NormalisedAlert

logger = logging.getLogger("alertmux.notify")


@dataclass
class RunReport:
    matched_by_rule: dict[str, int] = field(default_factory=dict)
    unevaluable_by_rule: dict[str, int] = field(default_factory=dict)
    sent_count: int = 0
    suppressed_by_rate_limit: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    dry_run_would_send: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """False whenever any delivery attempt failed this run. `cli.py`
        maps this to the process exit code -- a notifier that silently
        stops is worse than none (spec)."""
        return not self.failures


def filter_expired(
    alerts: list[NormalisedAlert], *, now: datetime | None = None
) -> list[NormalisedAlert]:
    """Drop alerts whose `expires` has already passed.

    Where `expires` is unavailable (`None`), the alert is kept -- the
    schema cannot distinguish "never expires" from "the source did not
    say" (see `unavailable_fields`/`unmapped_fields`, DECISIONS.md D18),
    and treating an unknown expiry as already-expired would silently
    withhold a possibly-still-live hazard, the wrong direction per
    requirement 4. An operator who wants strict expiry handling for a
    specific source can already see this in `unavailable_fields` if they
    build on this module.
    """
    now = now or datetime.now(tz=timezone.utc)
    return [a for a in alerts if a.expires is None or a.expires >= now]


def apply_cross_source_dedupe(
    alerts: list[NormalisedAlert], duplicate_groups: list[DuplicateGroup]
) -> list[NormalisedAlert]:
    """Collapse each duplicate group down to its `preferred_id` record
    only (D13). Every other member of a group is dropped from the
    candidate list entirely -- so a rule matching both SWIC and NWS's
    copy of the same NOAA warning notifies once, from the richer record,
    never once per source."""
    shadowed: set[str] = set()
    for group in duplicate_groups:
        for alert_id in group.alert_ids:
            if alert_id != group.preferred_id:
                shadowed.add(alert_id)
    return [a for a in alerts if a.id not in shadowed]


def run_once(
    response: AlertsResponse,
    rules: list[RuleConfig],
    state: StateStore,
    *,
    smtp_config: SmtpConfig | None = None,
    sender: SmtpSender | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RunReport:
    """Run one poll cycle against an already-collected `AlertsResponse`.

    `sender` is required unless `dry_run` is True. `smtp_config` supplies
    the From address; pass it explicitly (rather than reading
    `sender.config.sender`) so a dry run needs no SmtpSender at all.
    """
    if not dry_run and sender is None:
        raise ValueError("sender is required unless dry_run=True")
    if not dry_run and smtp_config is None:
        raise ValueError("smtp_config is required unless dry_run=True")

    now = now or datetime.now(tz=timezone.utc)
    report = RunReport(dry_run=dry_run)

    candidates = filter_expired(response.alerts, now=now)
    candidates = apply_cross_source_dedupe(candidates, response.duplicate_groups)

    warnings = warn_severity_rules_against_unmapped_sources(rules, candidates)
    for warning in warnings:
        logger.warning(warning)
    report.warnings = warnings

    from_address = smtp_config.sender if smtp_config is not None else "dry-run@localhost"

    for rule in rules:
        result = evaluate_rule(rule, candidates)
        report.matched_by_rule[rule.name] = len(result.matched)
        if result.unevaluable_count:
            report.unevaluable_by_rule[rule.name] = result.unevaluable_count
            logger.info(
                "rule '%s': %d alert(s) could not be evaluated for severity "
                "(unmapped source) -- see warnings for detail",
                rule.name,
                result.unevaluable_count,
            )

        new_alerts = [a for a in result.matched if not state.seen(a.id)]

        if rule.max_per_run is not None and len(new_alerts) > rule.max_per_run:
            suppressed = len(new_alerts) - rule.max_per_run
            report.suppressed_by_rate_limit[rule.name] = suppressed
            logger.warning(
                "rule '%s': rate limit hit -- %d of %d new alert(s) suppressed "
                "this run, will be retried next run (not recorded as notified)",
                rule.name,
                suppressed,
                len(new_alerts),
            )
            new_alerts = new_alerts[: rule.max_per_run]

        if not new_alerts:
            continue

        if dry_run:
            for alert in new_alerts:
                line = (
                    f"[dry-run] rule '{rule.name}' -> {', '.join(rule.to)}: "
                    f"{alert.event} ({alert.provenance.authority}, id={alert.id})"
                )
                report.dry_run_would_send.append(line)
                logger.info(line)
            continue

        assert sender is not None  # for type checkers; guarded above
        if rule.digest:
            message = build_digest_message(
                new_alerts, sender=from_address, to=list(rule.to), rule_name=rule.name
            )
            try:
                sender.send(message)
            except DeliveryError as exc:
                logger.error(
                    "delivery failed for rule '%s' digest of %d alert(s): %s",
                    rule.name,
                    len(new_alerts),
                    exc,
                )
                report.failures.append(f"rule '{rule.name}' digest ({len(new_alerts)} alerts): {exc}")
            else:
                for alert in new_alerts:
                    state.record(alert.id, rule=rule.name, when=now)
                report.sent_count += 1
        else:
            for alert in new_alerts:
                message = build_alert_message(
                    alert, sender=from_address, to=list(rule.to), rule_name=rule.name
                )
                try:
                    sender.send(message)
                except DeliveryError as exc:
                    logger.error(
                        "delivery failed for rule '%s' alert %s: %s", rule.name, alert.id, exc
                    )
                    report.failures.append(f"rule '{rule.name}' alert {alert.id}: {exc}")
                else:
                    state.record(alert.id, rule=rule.name, when=now)
                    report.sent_count += 1

    if not dry_run:
        state.save()

    return report
