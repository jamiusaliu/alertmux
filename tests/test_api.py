from datetime import datetime, timezone

from fastapi.testclient import TestClient

from alertmux.adapters.base import FetchResult
from alertmux.api import app, clear_cache, get_adapters
from alertmux.schema import NormalisedAlert, Provenance

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _alert():
    return NormalisedAlert(
        id="wmo-swic:1",
        event="THUNDERSTORMS",
        area_description="Some states in Nigeria will be affected.",
        source_severity="3",
        provenance=Provenance(
            authority="ng-nimet",
            source_id="wmo-swic",
            source_url="https://severeweather.wmo.int/g/wfs",
            retrieved_at=NOW,
        ),
        unavailable_fields=["severity"],
    )


class FakeAdapter:
    def __init__(self, source_id, ok=True, alerts=None, error=None):
        self.source_id = source_id
        self._result = FetchResult(
            source_id=source_id, ok=ok, alerts=alerts or [],
            error=error, retrieved_at=NOW, latency_ms=5,
        )

    def fetch(self):
        return self._result


def _client(adapters):
    app.dependency_overrides[get_adapters] = lambda: adapters
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()
    clear_cache()


def test_alerts_returns_normalised_alerts():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    body = client.get("/alerts").json()
    assert body["alerts"][0]["event"] == "THUNDERSTORMS"
    assert body["partial"] is False


def test_alerts_always_includes_provenance():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    provenance = client.get("/alerts").json()["alerts"][0]["provenance"]
    assert provenance["authority"] == "ng-nimet"
    assert provenance["source_url"].startswith("https://")
    assert provenance["retrieved_at"]


def test_alerts_never_reports_a_severity_it_was_not_given():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    alert = client.get("/alerts").json()["alerts"][0]
    assert alert["severity"] is None
    assert alert["source_severity"] == "3"


def test_alerts_flags_partial_when_a_source_fails():
    client = _client([
        FakeAdapter("wmo-swic", alerts=[_alert()]),
        FakeAdapter("usgs", ok=False, error="503"),
    ])
    body = client.get("/alerts").json()
    assert body["partial"] is True
    assert len(body["alerts"]) == 1


def test_alerts_filter_by_authority():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert len(client.get("/alerts?authority=ng-nimet").json()["alerts"]) == 1
    assert len(client.get("/alerts?authority=us-noaa").json()["alerts"]) == 0


def test_health_reports_every_source():
    client = _client([
        FakeAdapter("wmo-swic", alerts=[_alert()]),
        FakeAdapter("usgs", ok=False, error="timeout"),
    ])
    body = client.get("/health").json()
    assert body["ok"] is False
    by_id = {s["source_id"]: s for s in body["sources"]}
    assert by_id["wmo-swic"]["ok"] is True
    assert by_id["usgs"]["error"] == "timeout"


def test_health_ok_when_all_sources_healthy():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert client.get("/health").json()["ok"] is True


def test_health_does_not_return_alert_bodies():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert "alerts" not in client.get("/health").json()


def setup_function():
    clear_cache()


def test_health_returns_503_when_any_source_is_down():
    """{"ok": false} at HTTP 200 reads green to every standard monitor."""
    client = _client([
        FakeAdapter("wmo-swic", alerts=[_alert()]),
        FakeAdapter("usgs", ok=False, error="timeout"),
    ])
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["ok"] is False


def test_health_returns_200_when_all_sources_healthy():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert client.get("/health").status_code == 200


def test_unknown_authority_lists_the_authorities_actually_present():
    """A typo and a quiet day both return zero alerts. In this domain
    that ambiguity is a dangerous false negative."""
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    body = client.get("/alerts?authority=typo-does-not-exist").json()
    assert body["alerts"] == []
    assert body["available_authorities"] == ["ng-nimet"]


def test_known_authority_does_not_list_available_authorities():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    body = client.get("/alerts?authority=ng-nimet").json()
    assert len(body["alerts"]) == 1
    assert body["available_authorities"] is None


def test_both_endpoints_state_the_relay_disclaimer_in_the_body():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert "not a substitute" in client.get("/alerts").json()["disclaimer"].lower()
    clear_cache()
    assert "not a substitute" in client.get("/health").json()["disclaimer"].lower()


def test_truncated_source_makes_the_api_response_partial():
    truncated = FetchResult(
        source_id="wmo-swic", ok=True, alerts=[_alert()],
        retrieved_at=NOW, latency_ms=5, truncated=True, matched=2133, returned=1,
    )

    class Trunc:
        source_id = "wmo-swic"

        def fetch(self):
            return truncated

    body = _client([Trunc()]).get("/alerts").json()
    assert body["partial"] is True


def test_repeat_calls_within_the_ttl_do_not_refetch_the_sources():
    class Counting:
        source_id = "wmo-swic"

        def __init__(self):
            self.calls = 0

        def fetch(self):
            self.calls += 1
            return FetchResult(
                source_id=self.source_id, ok=True, alerts=[_alert()],
                retrieved_at=NOW, latency_ms=1,
            )

    adapter = Counting()
    client = _client([adapter])
    client.get("/health")
    client.get("/health")
    client.get("/alerts")
    assert adapter.calls == 1


def test_filtering_a_cached_response_does_not_poison_the_next_call():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert client.get("/alerts?authority=us-noaa").json()["alerts"] == []
    assert len(client.get("/alerts").json()["alerts"]) == 1


def test_health_does_not_poison_the_cache_for_a_later_alerts_call():
    """/health takes the shared cached object rather than a deep copy, so it
    must stay read-only. If it ever mutated the response, the next /alerts
    call would see the damage."""
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert client.get("/health").status_code == 200
    body = client.get("/alerts").json()
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["provenance"]["authority"] == "ng-nimet"


def test_alerts_filter_does_not_poison_a_later_health_call():
    """The reverse direction: /alerts still filters a private copy, so the
    sources /health reports are unaffected by an earlier filtered request."""
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert client.get("/alerts?authority=us-noaa").json()["alerts"] == []
    sources = client.get("/health").json()["sources"]
    assert [s["source_id"] for s in sources] == ["wmo-swic"]
    assert sources[0]["ok"] is True
