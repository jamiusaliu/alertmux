"""Unit tests for alertmux.notify.rules -- pure, no I/O."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alertmux.notify.rules import (
    RuleConfig,
    evaluate_rule,
    sources_with_no_mapped_severity,
    warn_severity_rules_against_unmapped_sources,
)
from alertmux.schema import NormalisedAlert, Provenance


def _alert(
    id="a1",
    event="Flood Warning",
    authority="ng-nimet",
    source_id="swic",
    severity=None,
    area_description="Lagos",
) -> NormalisedAlert:
    return NormalisedAlert(
        id=id,
        event=event,
        area_description=area_description,
        severity=severity,
        provenance=Provenance(
            authority=authority,
            source_id=source_id,
            source_url="https://example.org",
            retrieved_at=datetime.now(tz=timezone.utc),
        ),
    )


def test_no_filters_matches_everything():
    rule = RuleConfig(name="all", to=("ops@example.org",))
    result = evaluate_rule(rule, [_alert(), _alert(id="a2")])
    assert len(result.matched) == 2
    assert result.unevaluable_count == 0


def test_authority_filter():
    rule = RuleConfig(name="ng", to=("x",), authority="ng-nimet")
    alerts = [_alert(id="a1", authority="ng-nimet"), _alert(id="a2", authority="us-noaa")]
    result = evaluate_rule(rule, alerts)
    assert [a.id for a in result.matched] == ["a1"]


def test_source_filter():
    rule = RuleConfig(name="gdacs-only", to=("x",), source="gdacs")
    alerts = [_alert(id="a1", source_id="gdacs"), _alert(id="a2", source_id="usgs")]
    result = evaluate_rule(rule, alerts)
    assert [a.id for a in result.matched] == ["a1"]


def test_event_substring_filter_is_case_insensitive():
    rule = RuleConfig(name="quakes", to=("x",), event_contains="earthquake")
    alerts = [
        _alert(id="a1", event="Major EARTHQUAKE detected"),
        _alert(id="a2", event="Flood Warning"),
    ]
    result = evaluate_rule(rule, alerts)
    assert [a.id for a in result.matched] == ["a1"]


def test_area_substring_filter():
    rule = RuleConfig(name="lagos", to=("x",), area_contains="lagos")
    alerts = [
        _alert(id="a1", area_description="Greater Lagos Area"),
        _alert(id="a2", area_description="Abuja"),
    ]
    result = evaluate_rule(rule, alerts)
    assert [a.id for a in result.matched] == ["a1"]


def test_severity_threshold_matches_and_excludes():
    rule = RuleConfig(name="severe-plus", to=("x",), severity_at_least="Severe")
    alerts = [
        _alert(id="minor", severity="Minor"),
        _alert(id="severe", severity="Severe"),
        _alert(id="extreme", severity="Extreme"),
    ]
    result = evaluate_rule(rule, alerts)
    assert sorted(a.id for a in result.matched) == ["extreme", "severe"]
    assert result.unevaluable_count == 0


def test_combined_filters_require_all_to_pass():
    rule = RuleConfig(
        name="combo",
        to=("x",),
        authority="ng-nimet",
        event_contains="flood",
        severity_at_least="Moderate",
    )
    alerts = [
        _alert(id="match", authority="ng-nimet", event="Flood Warning", severity="Severe"),
        _alert(id="wrong-authority", authority="us-noaa", event="Flood Warning", severity="Severe"),
        _alert(id="wrong-event", authority="ng-nimet", event="Heat Advisory", severity="Severe"),
        _alert(id="too-low", authority="ng-nimet", event="Flood Warning", severity="Minor"),
    ]
    result = evaluate_rule(rule, alerts)
    assert [a.id for a in result.matched] == ["match"]


# --- The unmapped-severity design flaw: the core of this build ---


def test_severity_rule_counts_unevaluable_gdacs_shaped_alerts():
    """A GDACS-shaped alert (severity=None, source_severity='Green') must
    never be silently dropped from a severity rule's evaluation -- it must
    be counted."""
    rule = RuleConfig(name="severe-plus", to=("x",), severity_at_least="Severe")
    gdacs_alert = _alert(id="gdacs1", source_id="gdacs", severity=None)
    result = evaluate_rule(rule, [gdacs_alert])
    assert result.unevaluable_count == 1
    assert result.unevaluable_ids == ["gdacs1"]
    assert result.unevaluable_by_source == {"gdacs": 1}


def test_default_delivers_unevaluable_alerts_safe_direction():
    """Default behaviour: deliver rather than silently withhold (requirement 4)."""
    rule = RuleConfig(name="severe-plus", to=("x",), severity_at_least="Severe")
    gdacs_alert = _alert(id="gdacs1", source_id="gdacs", severity=None)
    result = evaluate_rule(rule, [gdacs_alert])
    assert [a.id for a in result.matched] == ["gdacs1"]
    assert result.unevaluable_count == 1


def test_explicit_opt_out_excludes_but_still_counts():
    rule = RuleConfig(
        name="severe-plus-strict",
        to=("x",),
        severity_at_least="Severe",
        include_unmapped_severity=False,
    )
    gdacs_alert = _alert(id="gdacs1", source_id="gdacs", severity=None)
    result = evaluate_rule(rule, [gdacs_alert])
    assert result.matched == []
    assert result.unevaluable_count == 1
    assert result.unevaluable_ids == ["gdacs1"]


def test_unknown_severity_is_treated_as_unevaluable_not_as_ranked():
    """NWS can legally emit severity='Unknown'. It must not be silently
    ranked as below Minor (excluded) or above Extreme (included) -- it is
    unrankable, same as None."""
    rule = RuleConfig(name="severe-plus", to=("x",), severity_at_least="Severe")
    alert = _alert(id="u1", severity="Unknown")
    result = evaluate_rule(rule, [alert])
    assert result.unevaluable_count == 1
    assert [a.id for a in result.matched] == ["u1"]


def test_sources_with_no_mapped_severity_flags_gdacs_shaped_source():
    alerts = [
        _alert(id="g1", source_id="gdacs", severity=None),
        _alert(id="g2", source_id="gdacs", severity=None),
        _alert(id="n1", source_id="swic", severity="Severe"),
    ]
    blind = sources_with_no_mapped_severity(alerts)
    assert blind == {"gdacs": 2}


def test_sources_with_no_mapped_severity_ignores_partially_mapped_source():
    alerts = [
        _alert(id="a1", source_id="swic", severity="Severe"),
        _alert(id="a2", source_id="swic", severity=None),
    ]
    assert sources_with_no_mapped_severity(alerts) == {}


def test_warning_fires_for_severity_rule_touching_unmapped_source():
    """Requirement 2: warn loudly, naming the affected source, when a
    severity rule would exclude a whole source."""
    rule = RuleConfig(name="global-severe", to=("x",), severity_at_least="Severe")
    alerts = [
        _alert(id="g1", source_id="gdacs", severity=None),
        _alert(id="t1", source_id="tsunami", severity=None),
        _alert(id="s1", source_id="swic", severity="Severe"),
    ]
    warnings = warn_severity_rules_against_unmapped_sources([rule], alerts)
    joined = "\n".join(warnings)
    assert "gdacs" in joined
    assert "tsunami" in joined
    assert "global-severe" in joined
    # swic has a rankable severity in this batch, so it should not appear
    # as a blind source.
    assert "'swic'" not in joined


def test_warning_absent_when_no_severity_rules():
    rule = RuleConfig(name="all-alerts", to=("x",))
    alerts = [_alert(id="g1", source_id="gdacs", severity=None)]
    assert warn_severity_rules_against_unmapped_sources([rule], alerts) == []


def test_warning_absent_when_no_unmapped_sources_present():
    rule = RuleConfig(name="severe-plus", to=("x",), severity_at_least="Severe")
    alerts = [_alert(id="s1", source_id="swic", severity="Severe")]
    assert warn_severity_rules_against_unmapped_sources([rule], alerts) == []


def test_warning_scoped_to_rules_own_source_filter():
    """A rule scoped to a specific source should not be warned about a
    different source's blindness."""
    rule = RuleConfig(
        name="ng-only-severe", to=("x",), severity_at_least="Severe", source="swic"
    )
    alerts = [
        _alert(id="g1", source_id="gdacs", severity=None),
        _alert(id="s1", source_id="swic", severity="Severe"),
    ]
    warnings = warn_severity_rules_against_unmapped_sources([rule], alerts)
    assert warnings == []


def test_warning_message_reflects_include_unmapped_severity_setting():
    included_rule = RuleConfig(name="r1", to=("x",), severity_at_least="Severe")
    excluded_rule = RuleConfig(
        name="r2", to=("x",), severity_at_least="Severe", include_unmapped_severity=False
    )
    alerts = [_alert(id="g1", source_id="gdacs", severity=None)]
    included_warnings = warn_severity_rules_against_unmapped_sources([included_rule], alerts)
    excluded_warnings = warn_severity_rules_against_unmapped_sources([excluded_rule], alerts)
    assert "still be delivered" in included_warnings[0]
    assert "SILENTLY EXCLUDED" in excluded_warnings[0]
