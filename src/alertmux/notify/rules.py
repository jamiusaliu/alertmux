"""Rule matching over `NormalisedAlert`. Pure functions, no I/O.

The hazard this module exists to prevent: a rule that says "notify on
Severe or above" silently delivering zero GDACS earthquakes and zero
tsunami bulletins, because GDACS's `alertlevel` and tsunami.gov's bulletin
category are both deliberately unmapped (DECISIONS.md D1/D17/D18) and so
`severity` is `None` for every alert those sources carry. A severity
threshold cannot be evaluated against a `None` severity, and silently
treating "cannot evaluate" as "does not match" would withhold exactly the
alerts a life-safety notifier exists to deliver.

Two things follow, both load-bearing:

1. Every rule evaluation counts the alerts it could not evaluate on
   severity, rather than silently dropping them from consideration
   (`RuleMatch.unevaluable_count`).
2. The default behaviour, absent an explicit operator choice, is to
   *deliver* an alert whose severity cannot be evaluated rather than
   withhold it -- see `RuleConfig.include_unmapped_severity` and its
   default of `True` in `config.py`. A false positive here costs an extra
   email; a false negative costs a missed hazard warning. That asymmetry
   is the same one DECISIONS.md D1 and D13 already apply elsewhere in this
   codebase, applied here to notification instead of translation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alertmux.schema import NormalisedAlert

# CAP's own ordinal scale, the same four values `swic.py`'s SEVERITY table
# and `nws.py` produce (D1). "Unknown" is a legal CAP value NWS can emit,
# but it is not orderable against a threshold any more than a `None`
# severity is -- so it is treated identically to "cannot evaluate" below,
# never silently coerced to the bottom or top of the scale.
SEVERITY_ORDER: dict[str, int] = {
    "Minor": 1,
    "Moderate": 2,
    "Severe": 3,
    "Extreme": 4,
}

# CAP severities that exist as named values but cannot be ranked against a
# threshold. Kept distinct from "None" only for documentation purposes --
# both are handled identically by `_severity_rank`.
UNRANKABLE_SEVERITIES = frozenset({"Unknown"})

# Safe-direction default for `RuleConfig.max_per_run` (see D22 in
# DECISIONS.md, extended). Verified live on 2026-08-18: the README's
# documented example config, with a single `severity_at_least = "Severe"`
# rule and no cap, reported "Dry run: 1137 alert(s) would be sent" on a
# first run -- 1,137 individual emails from the config this project tells
# people to copy. `None` (unlimited) must stay expressible for an operator
# who deliberately wants it, but it must never be the default: an
# unthrottled default is a mailbomb, per the spec's own framing ("A
# severe-weather day in the US NOAA set can produce thousands of alerts.
# An unthrottled notifier is a mailbomb"). 40 sits comfortably above what
# a normal poll interval produces for one rule while staying small enough
# that a runaway cannot flood a mailbox before the operator notices the
# loud warning `runner.py` prints when the cap is hit.
DEFAULT_MAX_PER_RUN = 40


@dataclass(frozen=True)
class RuleConfig:
    """One notification rule. Immutable, pure-data, no I/O anywhere near it.

    Filters are ANDed together; a filter left `None` is not applied. See
    `config.py` for how this is loaded from a file plus environment.
    """

    name: str
    to: tuple[str, ...]
    authority: str | None = None
    source: str | None = None
    event_contains: str | None = None
    area_contains: str | None = None
    severity_at_least: str | None = None
    # Safe-direction default (see module docstring): an alert whose
    # severity this rule cannot evaluate is still delivered unless the
    # operator explicitly opts out.
    include_unmapped_severity: bool = True
    # Safe-direction default (see `DEFAULT_MAX_PER_RUN` above): a rule
    # left unconfigured is capped, not unlimited. An operator who wants
    # no ceiling must set `max_per_run = 0` in TOML (there being no TOML
    # null) which `config.py` maps to Python `None` here.
    max_per_run: int | None = DEFAULT_MAX_PER_RUN
    digest: bool = False


@dataclass
class RuleMatch:
    """The result of evaluating one rule against a batch of alerts."""

    rule: RuleConfig
    matched: list[NormalisedAlert] = field(default_factory=list)
    # Alerts that passed every non-severity filter but whose severity
    # could not be ranked against `severity_at_least` (None or
    # "Unknown"). Counted regardless of whether they ended up included or
    # excluded -- see requirement 1 in the module docstring.
    unevaluable_count: int = 0
    unevaluable_ids: list[str] = field(default_factory=list)
    # source_id -> count of unevaluable alerts contributed by that source.
    unevaluable_by_source: dict[str, int] = field(default_factory=dict)


def _severity_rank(severity: str | None) -> int | None:
    if severity is None or severity in UNRANKABLE_SEVERITIES:
        return None
    return SEVERITY_ORDER.get(severity)


def _matches_non_severity_filters(rule: RuleConfig, alert: NormalisedAlert) -> bool:
    if rule.authority is not None and alert.provenance.authority != rule.authority:
        return False
    if rule.source is not None and alert.provenance.source_id != rule.source:
        return False
    if rule.event_contains is not None:
        if not alert.event or rule.event_contains.casefold() not in alert.event.casefold():
            return False
    if rule.area_contains is not None:
        if not alert.area_description or (
            rule.area_contains.casefold() not in alert.area_description.casefold()
        ):
            return False
    return True


def evaluate_rule(
    rule: RuleConfig, alerts: list[NormalisedAlert]
) -> RuleMatch:
    """Evaluate one rule against a batch of alerts.

    Pure and side-effect free: takes alerts, returns a result. No state
    file, no SMTP, no clock. Cross-source dedupe and expiry filtering are
    the runner's job (they apply to every rule, not per-rule), so callers
    are expected to pass in an already-deduplicated, already-unexpired
    alert list.
    """
    result = RuleMatch(rule=rule)

    for alert in alerts:
        if not _matches_non_severity_filters(rule, alert):
            continue

        if rule.severity_at_least is None:
            result.matched.append(alert)
            continue

        threshold_rank = SEVERITY_ORDER[rule.severity_at_least]
        rank = _severity_rank(alert.severity)

        if rank is None:
            result.unevaluable_count += 1
            result.unevaluable_ids.append(alert.id)
            source_id = alert.provenance.source_id
            result.unevaluable_by_source[source_id] = (
                result.unevaluable_by_source.get(source_id, 0) + 1
            )
            if rule.include_unmapped_severity:
                result.matched.append(alert)
            continue

        if rank >= threshold_rank:
            result.matched.append(alert)

    return result


def sources_with_no_mapped_severity(
    alerts: list[NormalisedAlert],
) -> dict[str, int]:
    """Sources where every alert in this batch has an unranked severity.

    Used for the startup/config-time warning (requirement 2): a severity
    rule that touches one of these sources will, for every single alert
    from it, fall into the unevaluable path above -- worth naming
    explicitly rather than only surfacing as a per-run count. Returns
    source_id -> alert count, for sources with at least one alert where
    none of that source's alerts in the batch carry a ranked severity.
    """
    total_by_source: dict[str, int] = {}
    unranked_by_source: dict[str, int] = {}

    for alert in alerts:
        source_id = alert.provenance.source_id
        total_by_source[source_id] = total_by_source.get(source_id, 0) + 1
        if _severity_rank(alert.severity) is None:
            unranked_by_source[source_id] = unranked_by_source.get(source_id, 0) + 1

    return {
        source_id: count
        for source_id, count in total_by_source.items()
        if unranked_by_source.get(source_id, 0) == count
    }


def warn_severity_rules_against_unmapped_sources(
    rules: list[RuleConfig], alerts: list[NormalisedAlert]
) -> list[str]:
    """Build the human-readable warnings for requirement 2.

    Returns one message per (rule, affected source) pair where the rule
    carries a severity threshold and the source never contributes a
    rankable severity in this batch. Pure -- callers decide whether to log
    it, print it, or both.
    """
    blind_sources = sources_with_no_mapped_severity(alerts)
    if not blind_sources:
        return []

    warnings: list[str] = []
    for rule in rules:
        if rule.severity_at_least is None:
            continue
        # Only warn about sources this rule could actually see (matching
        # its own source/authority filter, if any) -- a rule scoped to
        # ng-nimet has no reason to be warned about tsunami.gov.
        for source_id, count in sorted(blind_sources.items()):
            if rule.source is not None and rule.source != source_id:
                continue
            action = (
                "will still be delivered (include_unmapped_severity=true)"
                if rule.include_unmapped_severity
                else "will be SILENTLY EXCLUDED (include_unmapped_severity=false)"
            )
            warnings.append(
                f"rule '{rule.name}': severity_at_least={rule.severity_at_least!r} "
                f"cannot be evaluated for source '{source_id}' "
                f"({count} alert(s) in this fetch never carry a mapped severity); "
                f"{action}"
            )
    return warnings
