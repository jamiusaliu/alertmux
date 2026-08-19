from datetime import datetime, timezone

from fastapi.testclient import TestClient

from alertmux.adapters.base import FetchResult
from alertmux import api
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
    def __init__(
        self, source_id, ok=True, alerts=None, error=None,
        invalid_count=0, invalid_samples=None,
    ):
        self.source_id = source_id
        self._result = FetchResult(
            source_id=source_id, ok=ok, alerts=alerts or [],
            error=error, retrieved_at=NOW, latency_ms=5,
            invalid_count=invalid_count, invalid_samples=invalid_samples or [],
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


def test_alerts_surfaces_unmapped_fields_distinct_from_unavailable():
    """unmapped_fields (a value was supplied and declined) must reach
    the /alerts JSON body just like unavailable_fields (nothing was
    supplied), and the two must never overlap for the same alert."""
    unmapped = _alert().model_copy(
        update={"unavailable_fields": [], "unmapped_fields": ["severity"]}
    )
    client = _client([FakeAdapter("wmo-swic", alerts=[unmapped])])
    alert = client.get("/alerts").json()["alerts"][0]
    assert alert["unmapped_fields"] == ["severity"]
    assert "severity" not in alert["unavailable_fields"]


def _dupe_alert(alert_id: str, source_id: str) -> NormalisedAlert:
    return NormalisedAlert(
        id=alert_id,
        event="Heat Advisory",
        area_description="Cook County, IL",
        provenance=Provenance(
            authority="us-noaa",
            source_id=source_id,
            source_url="https://example.test",
            retrieved_at=NOW,
        ),
    )


def test_alerts_returns_every_record_even_when_duplicate_groups_are_reported():
    """Grouping is a report, not a filter -- /alerts must never shrink."""
    client = _client([
        FakeAdapter("wmo-swic", alerts=[_dupe_alert("wmo-swic:1", "wmo-swic")]),
        FakeAdapter("nws", alerts=[_dupe_alert("nws:1", "nws")]),
    ])
    body = client.get("/alerts").json()
    assert len(body["alerts"]) == 2
    assert len(body["duplicate_groups"]) == 1
    assert set(body["duplicate_groups"][0]["alert_ids"]) == {
        "wmo-swic:1",
        "nws:1",
    }


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


def test_unknown_authority_on_a_complete_fetch_is_not_flagged_partial():
    """Issue #9: a complete fetch's available_authorities list can be
    trusted -- the flag must say so."""
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    body = client.get("/alerts?authority=typo-does-not-exist").json()
    assert body["partial"] is False
    assert body["available_authorities_partial"] is False


def test_unknown_authority_on_a_partial_fetch_is_flagged_partial():
    """Issue #9: a source being down must not let a short authority list
    read as complete. ng-nimet is a real, known-good authority here --
    it is simply absent from this fetch because usgs (a different
    source) failed. The response must not imply ng-nimet-like slugs
    from the down source do not exist."""
    client = _client([
        FakeAdapter("wmo-swic", alerts=[_alert()]),
        FakeAdapter("usgs", ok=False, error="timeout"),
    ])
    body = client.get("/alerts?authority=typo-does-not-exist").json()
    assert body["partial"] is True
    assert body["available_authorities_partial"] is True
    # The authority list is still whatever the fetch actually saw --
    # never invented -- just explicitly marked as possibly short.
    assert body["available_authorities"] == ["ng-nimet"]


def test_known_authority_does_not_set_available_authorities_partial():
    """The flag only exists alongside available_authorities; when the
    filter matched, there is nothing for it to qualify."""
    client = _client([
        FakeAdapter("wmo-swic", alerts=[_alert()]),
        FakeAdapter("usgs", ok=False, error="timeout"),
    ])
    body = client.get("/alerts?authority=ng-nimet").json()
    assert body["available_authorities"] is None
    assert body["available_authorities_partial"] is None


def test_both_endpoints_state_the_relay_disclaimer_in_the_body():
    client = _client([FakeAdapter("wmo-swic", alerts=[_alert()])])
    assert "not a substitute" in client.get("/alerts").json()["disclaimer"].lower()
    clear_cache()
    assert "not a substitute" in client.get("/health").json()["disclaimer"].lower()


def test_alerts_exposes_invalid_count_and_forces_partial():
    """D3: a source that quarantined records must be visible to a
    consumer of /alerts, not just internally."""
    client = _client([
        FakeAdapter(
            "wmo-swic", alerts=[_alert()],
            invalid_count=1, invalid_samples=["ValueError: no capurl"],
        ),
    ])
    body = client.get("/alerts").json()
    assert body["partial"] is True
    source = next(s for s in body["sources"] if s["source_id"] == "wmo-swic")
    assert source["ok"] is True
    assert source["invalid_count"] == 1
    assert source["invalid_samples"] == ["ValueError: no capurl"]


def test_health_exposes_invalid_count_and_returns_503():
    client = _client([
        FakeAdapter(
            "wmo-swic", alerts=[_alert()],
            invalid_count=2, invalid_samples=["ValueError: no capurl"],
        ),
    ])
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    source = next(s for s in body["sources"] if s["source_id"] == "wmo-swic")
    assert source["invalid_count"] == 2
    assert source["invalid_samples"] == ["ValueError: no capurl"]


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


# ---------------------------------------------------------------------------
# GET /alerts/{alert_id}/detail (issue #4)
# ---------------------------------------------------------------------------

from alertmux.adapters.swic import CapDetail, CapDetailError  # noqa: E402


def _swic_alert(alert_id="wmo-swic:ng-nimet-en/a.xml", raw_reference="ng-nimet-en/a.xml"):
    return NormalisedAlert(
        id=alert_id,
        event="THUNDERSTORMS",
        area_description="Some states in Nigeria will be affected.",
        source_severity="3",
        severity="Severe",
        provenance=Provenance(
            authority="ng-nimet",
            source_id="wmo-swic",
            source_url="https://severeweather.wmo.int/g/wfs",
            retrieved_at=NOW,
            raw_reference=raw_reference,
        ),
        unavailable_fields=["headline", "description", "instruction", "onset", "expires"],
    )


class FakeSwicAdapter(FakeAdapter):
    """A FakeAdapter that also supports fetch_detail, like SwicAdapter."""

    def __init__(self, *args, detail=None, detail_error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._detail = detail
        self._detail_error = detail_error
        self.detail_calls = 0

    def fetch_detail(self, capurl):
        self.detail_calls += 1
        if self._detail_error is not None:
            raise self._detail_error
        return self._detail


def _detail():
    return CapDetail(
        headline="THUNDERSTORMS OVER PARTS OF NIGERIA",
        instruction="Take shelter.",
        severity="Extreme",
        urgency="Immediate",
        certainty="Observed",
        expires=datetime(2026, 8, 18, 5, 0, tzinfo=timezone.utc),
        onset=datetime(2026, 8, 17, 18, 16, tzinfo=timezone.utc),
    )


def test_alert_detail_enriches_and_shrinks_unavailable_fields():
    alert = _swic_alert()
    client = _client([FakeSwicAdapter("wmo-swic", alerts=[alert], detail=_detail())])
    body = client.get(f"/alerts/{alert.id}/detail").json()
    assert body["headline"] == "THUNDERSTORMS OVER PARTS OF NIGERIA"
    assert body["instruction"] == "Take shelter."
    assert body["expires"] is not None
    assert "headline" not in body["unavailable_fields"]
    assert "expires" not in body["unavailable_fields"]


def test_alert_detail_cap_severity_wins_over_list_view_code():
    alert = _swic_alert()
    client = _client([FakeSwicAdapter("wmo-swic", alerts=[alert], detail=_detail())])
    body = client.get(f"/alerts/{alert.id}/detail").json()
    assert body["severity"] == "Extreme"
    assert body["source_severity"] == "3"


def test_alert_detail_clears_severity_from_unmapped_fields_not_unavailable():
    """An unverified list-view code (unmapped_fields) is resolved by the
    signed CAP file exactly like a structurally absent field
    (unavailable_fields) -- both routes through the same /detail
    endpoint, and unmapped_fields is a JSON field alongside
    unavailable_fields on the response body."""
    alert = _swic_alert()
    alert = alert.model_copy(
        update={
            "severity": None,
            "source_severity": "0",
            "unmapped_fields": ["severity"],
        }
    )
    client = _client([FakeSwicAdapter("wmo-swic", alerts=[alert], detail=_detail())])
    body = client.get(f"/alerts/{alert.id}/detail").json()
    assert body["severity"] == "Extreme"
    assert "severity" not in body["unmapped_fields"]
    assert "severity" not in body["unavailable_fields"]


def test_alert_detail_unknown_id_is_404():
    client = _client([FakeSwicAdapter("wmo-swic", alerts=[_swic_alert()], detail=_detail())])
    response = client.get("/alerts/does-not-exist/detail")
    assert response.status_code == 404


def test_alert_detail_cap_fetch_failure_is_a_clear_error_not_empty():
    alert = _swic_alert()
    client = _client([
        FakeSwicAdapter(
            "wmo-swic", alerts=[alert],
            detail_error=CapDetailError("SWIC CAP detail fetch failed: 404"),
        ),
    ])
    response = client.get(f"/alerts/{alert.id}/detail")
    assert response.status_code == 502
    assert "404" in response.json()["detail"]


def test_alert_detail_non_swic_source_is_404():
    alert = _dupe_alert("nws:1", "nws")
    client = _client([FakeAdapter("nws", alerts=[alert])])
    response = client.get(f"/alerts/{alert.id}/detail")
    assert response.status_code == 404


def test_alert_detail_resolves_raw_reference_not_the_alert_id():
    """The endpoint must fetch by provenance.raw_reference (the capurl),
    not by the alertmux-internal id -- CapDetail's cache and WMO's own
    endpoint are both keyed by capurl. Caching itself is
    SwicAdapter.fetch_detail's responsibility, exercised directly in
    test_swic.py's test_fetch_detail_caches_and_never_fetches_twice.
    """
    alert = _swic_alert(alert_id="wmo-swic:ng-nimet-en/a.xml", raw_reference="ng-nimet-en/a.xml")
    adapter = FakeSwicAdapter("wmo-swic", alerts=[alert], detail=_detail())
    original_fetch_detail = adapter.fetch_detail
    seen_capurls = []

    def _spy(capurl):
        seen_capurls.append(capurl)
        return original_fetch_detail(capurl)

    adapter.fetch_detail = _spy
    client = _client([adapter])
    client.get(f"/alerts/{alert.id}/detail")
    assert seen_capurls == ["ng-nimet-en/a.xml"]


def test_alerts_endpoint_unaffected_by_detail_route_existing():
    """The default /alerts behaviour must stay cheap and unchanged."""
    alert = _swic_alert()
    client = _client([FakeSwicAdapter("wmo-swic", alerts=[alert], detail=_detail())])
    body = client.get("/alerts").json()
    assert body["alerts"][0]["headline"] is None
    assert "headline" in body["alerts"][0]["unavailable_fields"]


class _Recovering:
    """Fails the first fetch, succeeds on every later one."""

    source_id = "wmo-swic"

    def __init__(self):
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.calls == 1:
            return FetchResult(
                source_id=self.source_id, ok=False, error="down",
                retrieved_at=NOW, latency_ms=1,
            )
        return FetchResult(
            source_id=self.source_id, ok=True, alerts=[_alert()],
            retrieved_at=NOW, latency_ms=1,
        )


def _frozen_clock(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(api.time, "monotonic", lambda: clock["t"])
    return clock


def test_a_degraded_fetch_is_rechecked_before_the_full_ttl(monkeypatch):
    """A source that recovers must stop being reported as down within
    PARTIAL_CACHE_TTL_SECONDS, not the full minute."""
    clock = _frozen_clock(monkeypatch)
    adapter = _Recovering()
    client = _client([adapter])

    assert client.get("/health").status_code == 503

    clock["t"] += api.PARTIAL_CACHE_TTL_SECONDS + 1
    assert client.get("/health").status_code == 200
    assert adapter.calls == 2


def test_a_degraded_fetch_is_still_cached_within_its_shorter_ttl(monkeypatch):
    """Control: the shorter TTL must still be a cache. A flapping source
    would otherwise refetch on every single request."""
    clock = _frozen_clock(monkeypatch)
    adapter = _Recovering()
    client = _client([adapter])

    assert client.get("/health").status_code == 503
    clock["t"] += api.PARTIAL_CACHE_TTL_SECONDS - 1
    assert client.get("/health").status_code == 503
    assert adapter.calls == 1


def test_a_healthy_fetch_is_not_rechecked_at_the_degraded_interval(monkeypatch):
    """Control in the other direction: the shorter TTL must apply only to
    degraded responses, or the cache loses most of its value."""
    clock = _frozen_clock(monkeypatch)

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
    clock["t"] += api.PARTIAL_CACHE_TTL_SECONDS + 1
    client.get("/health")
    assert adapter.calls == 1
