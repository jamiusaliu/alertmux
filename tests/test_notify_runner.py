"""Tests for alertmux.notify.runner -- the orchestration layer.

Uses unittest.mock in place of a real SmtpSender; nothing here touches a
socket.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from alertmux.dedupe import DuplicateGroup
from alertmux.notify.config import SmtpConfig
from alertmux.notify.delivery import DeliveryError
from alertmux.notify.rules import RuleConfig
from alertmux.notify.runner import apply_cross_source_dedupe, filter_expired, run_once
from alertmux.notify.state import StateStore
from alertmux.query import AlertsResponse
from alertmux.schema import NormalisedAlert, Provenance

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _alert(
    id,
    event="Flood Warning",
    authority="ng-nimet",
    source_id="swic",
    severity="Severe",
    expires=None,
) -> NormalisedAlert:
    return NormalisedAlert(
        id=id,
        event=event,
        headline=f"Headline for {id}",
        description=f"Description for {id}",
        area_description="Lagos",
        severity=severity,
        provenance=Provenance(
            authority=authority,
            source_id=source_id,
            source_url="https://example.org",
            retrieved_at=NOW,
        ),
        expires=expires,
    )


def _response(alerts, duplicate_groups=None) -> AlertsResponse:
    return AlertsResponse(
        alerts=alerts,
        retrieved_at=NOW,
        duplicate_groups=duplicate_groups or [],
    )


def _smtp_config():
    return SmtpConfig(host="smtp.example.org", sender="alerts@example.org")


def _state(tmp_path) -> StateStore:
    store = StateStore(tmp_path / "state.json")
    store.load()
    return store


# --- filter_expired ---


def test_filter_expired_drops_only_past_expiry():
    live = _alert("live", expires=NOW + timedelta(hours=1))
    expired = _alert("expired", expires=NOW - timedelta(hours=1))
    unknown = _alert("unknown", expires=None)
    kept = filter_expired([live, expired, unknown], now=NOW)
    assert {a.id for a in kept} == {"live", "unknown"}


# --- apply_cross_source_dedupe ---


def test_dedupe_keeps_only_preferred_record():
    swic = _alert("swic:1", source_id="swic")
    nws = _alert("nws:1", source_id="nws")
    group = DuplicateGroup(
        key="flood warning|lagos",
        alert_ids=["nws:1", "swic:1"],
        preferred_id="nws:1",
        reason="richer",
    )
    kept = apply_cross_source_dedupe([swic, nws], [group])
    assert {a.id for a in kept} == {"nws:1"}


# --- end-to-end via run_once ---


def test_notifies_once_from_preferred_record_in_duplicate_group(tmp_path):
    swic = _alert("swic:1", source_id="swic")
    nws = _alert("nws:1", source_id="nws")
    group = DuplicateGroup(
        key="flood warning|lagos", alert_ids=["nws:1", "swic:1"], preferred_id="nws:1", reason="r"
    )
    response = _response([swic, nws], duplicate_groups=[group])
    rule = RuleConfig(name="all", to=("ops@example.org",))
    state = _state(tmp_path)
    sender = MagicMock()

    report = run_once(response, [rule], state, smtp_config=_smtp_config(), sender=sender, now=NOW)

    assert sender.send.call_count == 1
    assert report.sent_count == 1
    assert report.ok


def test_expired_alert_never_notified(tmp_path):
    expired = _alert("e1", expires=NOW - timedelta(minutes=1))
    response = _response([expired])
    rule = RuleConfig(name="all", to=("ops@example.org",))
    state = _state(tmp_path)
    sender = MagicMock()

    report = run_once(response, [rule], state, smtp_config=_smtp_config(), sender=sender, now=NOW)

    sender.send.assert_not_called()
    assert report.sent_count == 0
    assert report.matched_by_rule["all"] == 0


def test_alert_notified_once_not_notified_again_next_run(tmp_path):
    alert = _alert("a1")
    rule = RuleConfig(name="all", to=("ops@example.org",))
    path = tmp_path / "state.json"

    state1 = StateStore(path)
    state1.load()
    sender1 = MagicMock()
    run_once(_response([alert]), [rule], state1, smtp_config=_smtp_config(), sender=sender1, now=NOW)
    assert sender1.send.call_count == 1

    state2 = StateStore(path)
    state2.load()
    sender2 = MagicMock()
    run_once(_response([alert]), [rule], state2, smtp_config=_smtp_config(), sender=sender2, now=NOW)
    assert sender2.send.call_count == 0


def test_rate_limit_caps_a_flood(tmp_path):
    alerts = [_alert(f"a{i}") for i in range(10)]
    rule = RuleConfig(name="capped", to=("ops@example.org",), max_per_run=3)
    state = _state(tmp_path)
    sender = MagicMock()

    report = run_once(_response(alerts), [rule], state, smtp_config=_smtp_config(), sender=sender, now=NOW)

    assert sender.send.call_count == 3
    assert report.sent_count == 3
    assert report.suppressed_by_rate_limit["capped"] == 7
    # Suppressed alerts were not recorded as notified, so they're
    # retried (up to the cap again) next run.
    assert sum(1 for a in alerts if state.seen(a.id)) == 3


def test_digest_batches_into_one_message(tmp_path):
    alerts = [_alert(f"a{i}") for i in range(5)]
    rule = RuleConfig(name="digest-rule", to=("ops@example.org",), digest=True)
    state = _state(tmp_path)
    sender = MagicMock()

    report = run_once(_response(alerts), [rule], state, smtp_config=_smtp_config(), sender=sender, now=NOW)

    assert sender.send.call_count == 1
    assert report.sent_count == 1
    assert all(state.seen(a.id) for a in alerts)


def test_default_max_per_run_caps_a_large_first_run(tmp_path):
    """A rule built with no `max_per_run` at all -- the shape an operator
    gets from copying the README's example config -- must not deliver an
    unbounded batch. Verified live on 2026-08-18: this exact shape (a
    single `severity_at_least` rule, no cap) reported "1137 alert(s)
    would be sent" against real NOAA data. This test stands in for that
    with a smaller, deterministic batch."""
    alerts = [_alert(f"a{i}") for i in range(200)]
    rule = RuleConfig(name="uncapped-by-operator", to=("ops@example.org",))
    state = _state(tmp_path)
    sender = MagicMock()

    report = run_once(_response(alerts), [rule], state, smtp_config=_smtp_config(), sender=sender, now=NOW)

    assert rule.max_per_run == 40  # rules.DEFAULT_MAX_PER_RUN
    assert sender.send.call_count == 40
    assert report.sent_count == 40
    assert report.suppressed_by_rate_limit["uncapped-by-operator"] == 160


def test_max_per_run_none_is_still_unlimited_when_set_explicitly(tmp_path):
    """An operator who deliberately opts out of the ceiling by passing
    `max_per_run=None` explicitly must get exactly that -- the safe
    default only applies when the operator leaves the field unset."""
    alerts = [_alert(f"a{i}") for i in range(200)]
    rule = RuleConfig(name="deliberately-unlimited", to=("ops@example.org",), max_per_run=None)
    state = _state(tmp_path)
    sender = MagicMock()

    report = run_once(_response(alerts), [rule], state, smtp_config=_smtp_config(), sender=sender, now=NOW)

    assert sender.send.call_count == 200
    assert report.sent_count == 200
    assert "deliberately-unlimited" not in report.suppressed_by_rate_limit


def test_run_below_cap_is_unaffected_and_produces_no_suppression(tmp_path):
    alerts = [_alert(f"a{i}") for i in range(5)]
    rule = RuleConfig(name="under-cap", to=("ops@example.org",))  # default cap of 40
    state = _state(tmp_path)
    sender = MagicMock()

    report = run_once(_response(alerts), [rule], state, smtp_config=_smtp_config(), sender=sender, now=NOW)

    assert sender.send.call_count == 5
    assert report.sent_count == 5
    assert report.suppressed_by_rate_limit == {}


def test_cap_applies_before_digest_so_digest_reflects_the_capped_set(tmp_path):
    """Decision: the per-run cap applies first, then digest batches
    whatever survives the cap into one email -- so a rule with both
    `max_per_run` and `digest = true` never digests more alerts than the
    cap allows in a single run. Documented in DECISIONS.md's extension
    to D22."""
    alerts = [_alert(f"a{i}") for i in range(10)]
    rule = RuleConfig(name="digest-capped", to=("ops@example.org",), max_per_run=3, digest=True)
    state = _state(tmp_path)
    sender = MagicMock()

    report = run_once(_response(alerts), [rule], state, smtp_config=_smtp_config(), sender=sender, now=NOW)

    # One digest email, for exactly the 3 alerts the cap allowed through.
    assert sender.send.call_count == 1
    assert report.sent_count == 1
    assert report.suppressed_by_rate_limit["digest-capped"] == 7
    assert sum(1 for a in alerts if state.seen(a.id)) == 3


def test_smtp_failure_is_loud_not_swallowed(tmp_path):
    alert = _alert("a1")
    rule = RuleConfig(name="r1", to=("ops@example.org",))
    state = _state(tmp_path)
    sender = MagicMock()
    sender.send.side_effect = DeliveryError("SMTP delivery to smtp.example.org:587 failed: boom")

    report = run_once(_response([alert]), [rule], state, smtp_config=_smtp_config(), sender=sender, now=NOW)

    assert not report.ok
    assert report.failures
    assert "boom" in report.failures[0]
    # Not recorded as notified -- must be retried next run.
    assert not state.seen("a1")


def test_smtp_failure_leaves_alert_for_retry_next_run(tmp_path):
    alert = _alert("a1")
    rule = RuleConfig(name="r1", to=("ops@example.org",))
    path = tmp_path / "state.json"

    failing_sender = MagicMock()
    failing_sender.send.side_effect = DeliveryError("boom")
    state1 = StateStore(path)
    state1.load()
    run_once(_response([alert]), [rule], state1, smtp_config=_smtp_config(), sender=failing_sender, now=NOW)

    working_sender = MagicMock()
    state2 = StateStore(path)
    state2.load()
    report2 = run_once(_response([alert]), [rule], state2, smtp_config=_smtp_config(), sender=working_sender, now=NOW)

    assert working_sender.send.call_count == 1
    assert report2.sent_count == 1


def test_dry_run_sends_nothing(tmp_path):
    alert = _alert("a1")
    rule = RuleConfig(name="r1", to=("ops@example.org",))
    state = _state(tmp_path)

    report = run_once(_response([alert]), [rule], state, dry_run=True, now=NOW)

    assert report.sent_count == 0
    assert report.dry_run_would_send
    # dry-run never persists state -- a real run afterwards still sends.
    assert not state.seen("a1")


def test_dry_run_never_calls_sender_even_if_provided(tmp_path):
    alert = _alert("a1")
    rule = RuleConfig(name="r1", to=("ops@example.org",))
    state = _state(tmp_path)
    sender = MagicMock()

    run_once(_response([alert]), [rule], state, sender=sender, dry_run=True, now=NOW)

    sender.send.assert_not_called()


def test_dry_run_still_surfaces_unmapped_severity_warning(tmp_path):
    gdacs_alert = _alert("g1", source_id="gdacs", severity=None)
    rule = RuleConfig(name="severe-only", to=("ops@example.org",), severity_at_least="Severe")
    state = _state(tmp_path)

    report = run_once(_response([gdacs_alert]), [rule], state, dry_run=True, now=NOW)

    assert report.warnings
    assert any("gdacs" in w for w in report.warnings)
    # Safe default still delivers it in the dry-run preview.
    assert report.dry_run_would_send
