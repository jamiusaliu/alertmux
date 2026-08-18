from datetime import datetime, timezone

from alertmux.adapters.base import FetchResult
from alertmux.query import collect
from alertmux.schema import NormalisedAlert, Provenance

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _alert(alert_id: str, source_id: str) -> NormalisedAlert:
    return NormalisedAlert(
        id=alert_id,
        event="THUNDERSTORMS",
        provenance=Provenance(
            authority="ng-nimet",
            source_id=source_id,
            source_url="https://example.test",
            retrieved_at=NOW,
        ),
    )


class FakeAdapter:
    def __init__(self, source_id, ok=True, alerts=None, error=None):
        self.source_id = source_id
        self._result = FetchResult(
            source_id=source_id,
            ok=ok,
            alerts=alerts or [],
            error=error,
            retrieved_at=NOW,
            latency_ms=10,
        )

    def fetch(self):
        return self._result


def test_collect_merges_alerts_from_all_sources():
    response = collect([
        FakeAdapter("a", alerts=[_alert("a:1", "a")]),
        FakeAdapter("b", alerts=[_alert("b:1", "b"), _alert("b:2", "b")]),
    ])
    assert len(response.alerts) == 3


def test_collect_is_not_partial_when_all_sources_succeed():
    response = collect([FakeAdapter("a"), FakeAdapter("b")])
    assert response.partial is False


def test_collect_is_partial_when_any_source_fails():
    """Silent partial success is a bug. The response must say so."""
    response = collect([
        FakeAdapter("a", alerts=[_alert("a:1", "a")]),
        FakeAdapter("b", ok=False, error="503"),
    ])
    assert response.partial is True


def test_failing_source_does_not_suppress_healthy_ones():
    response = collect([
        FakeAdapter("a", alerts=[_alert("a:1", "a")]),
        FakeAdapter("b", ok=False, error="timeout"),
    ])
    assert len(response.alerts) == 1


def test_every_source_is_reported_with_its_status():
    response = collect([
        FakeAdapter("a", alerts=[_alert("a:1", "a")]),
        FakeAdapter("b", ok=False, error="timeout"),
    ])
    by_id = {s.source_id: s for s in response.sources}
    assert by_id["a"].ok is True
    assert by_id["a"].alert_count == 1
    assert by_id["b"].ok is False
    assert by_id["b"].error == "timeout"


def test_adapter_that_raises_is_reported_not_propagated():
    class Exploding:
        source_id = "boom"

        def fetch(self):
            raise RuntimeError("kaboom")

    response = collect([Exploding()])
    assert response.partial is True
    assert response.sources[0].ok is False
    assert "kaboom" in response.sources[0].error


def test_adapter_without_a_source_id_is_reported_as_unknown():
    """collect() falls back to "unknown" so the failure handler cannot itself
    raise on an adapter too broken to carry a source_id. Exploding above sets
    one, so that branch is never reached by any other test."""

    class Nameless:
        def fetch(self):
            raise RuntimeError("kaboom")

    response = collect([Nameless()])
    assert response.partial is True
    assert response.sources[0].source_id == "unknown"
    assert response.sources[0].ok is False
    assert "kaboom" in response.sources[0].error


def test_the_unknown_fallback_does_not_override_a_real_source_id():
    """Control for the test above: an adapter that does carry a source_id must
    still be reported under its own name when it raises."""

    class Named:
        source_id = "gdacs"

        def fetch(self):
            raise RuntimeError("kaboom")

    assert collect([Named()]).sources[0].source_id == "gdacs"


def test_all_sources_down_returns_empty_and_partial():
    response = collect([
        FakeAdapter("a", ok=False, error="down"),
        FakeAdapter("b", ok=False, error="down"),
    ])
    assert response.alerts == []
    assert response.partial is True


class TruncatedAdapter:
    """ok=True, but the source held more alerts than it returned."""

    source_id = "wmo-swic"

    def fetch(self):
        return FetchResult(
            source_id=self.source_id,
            ok=True,
            alerts=[_alert("wmo-swic:1", "wmo-swic")],
            retrieved_at=NOW,
            latency_ms=10,
            truncated=True,
            matched=2133,
            returned=1,
        )


def test_truncated_but_ok_source_makes_the_response_partial():
    """Dropping alerts while reporting partial=false is silent partial
    success — the exact failure this project exists to prevent."""
    response = collect([TruncatedAdapter()])
    assert response.sources[0].ok is True
    assert response.sources[0].truncated is True
    assert response.sources[0].matched == 2133
    assert response.partial is True


def test_response_carries_the_relay_disclaimer():
    response = collect([FakeAdapter("a")])
    assert "not a substitute" in response.disclaimer.lower()
