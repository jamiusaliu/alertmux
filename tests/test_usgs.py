import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from alertmux.adapters.usgs import UsgsAdapter

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "usgs_4.5_day.json").read_text()
)
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def test_parse_returns_one_alert_per_feature():
    alerts = UsgsAdapter().parse(FIXTURE, NOW)
    assert len(alerts) == 2


def test_parse_maps_identity_and_event():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.id == "usgs:us6000tlnv"
    assert alert.event == "earthquake"
    assert alert.headline == "M 4.9 - 79 km N of Ruteng, Indonesia"
    assert alert.area_description == "79 km N of Ruteng, Indonesia"


def test_parse_converts_epoch_millis_to_utc_datetime():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.sent == datetime.fromtimestamp(1787054630174 / 1000, tz=timezone.utc)


def test_parse_preserves_geometry():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.geometry["type"] == "Point"
    assert alert.geometry["coordinates"][0] == pytest.approx(120.5751)


def test_parse_sets_provenance():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.provenance.authority == "us-usgs"
    assert alert.provenance.source_id == "usgs"
    assert alert.provenance.retrieved_at == NOW
    assert alert.provenance.raw_reference.endswith("us6000tlnv")


def test_null_alert_level_is_recorded_as_unavailable_not_guessed():
    """No PAGER level at all -- the source said nothing, so severity is
    unavailable, not unmapped."""
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.severity is None
    assert alert.source_severity is None
    assert "severity" in alert.unavailable_fields
    assert "severity" not in alert.unmapped_fields


def test_present_alert_level_is_kept_as_source_severity_only():
    """A PAGER level IS present -- the source said something, and it is
    declined on principle (not CAP severity), so severity is unmapped,
    not unavailable. FIXTURE[1] is the live M5.7 Mexico quake, PAGER
    level green — the sort of significant event this feed exists to
    carry."""
    alert = UsgsAdapter().parse(FIXTURE, NOW)[1]
    assert alert.source_severity == "green"
    assert alert.severity is None
    assert "severity" in alert.unmapped_fields
    assert "severity" not in alert.unavailable_fields


def test_default_feed_is_the_magnitude_45_past_day_summary():
    """The default feed is upstream magnitude-thresholded (M4.5+, past
    day), not all_hour — see DECISIONS.md D19. The class attribute stays
    the default so tests and /sources see the same URL as a default
    instance."""
    assert UsgsAdapter.URL == (
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/"
        "summary/4.5_day.geojson"
    )
    assert UsgsAdapter().URL == UsgsAdapter.URL


def test_constructor_feed_override_changes_the_url():
    adapter = UsgsAdapter(feed="significant_week")
    assert adapter.URL == (
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/"
        "summary/significant_week.geojson"
    )


@respx.mock
def test_fetch_uses_the_configured_feed_url():
    adapter = UsgsAdapter(feed="4.5_week")
    respx.get(adapter.URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    result = adapter.fetch()
    assert result.ok is True
    assert result.alerts[0].provenance.source_url == adapter.URL


def test_usgs_never_supplies_expiry():
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.expires is None
    assert "expires" in alert.unavailable_fields


@respx.mock
def test_fetch_returns_ok_result():
    respx.get(UsgsAdapter.URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    result = UsgsAdapter().fetch()
    assert result.ok is True
    assert result.source_id == "usgs"
    assert len(result.alerts) == 2
    assert result.error is None


@respx.mock
def test_fetch_reports_http_error_without_raising():
    respx.get(UsgsAdapter.URL).mock(return_value=httpx.Response(503))
    result = UsgsAdapter().fetch()
    assert result.ok is False
    assert result.alerts == []
    assert "503" in result.error


@respx.mock
def test_fetch_reports_timeout_without_raising():
    respx.get(UsgsAdapter.URL).mock(side_effect=httpx.TimeoutException("timed out"))
    result = UsgsAdapter().fetch()
    assert result.ok is False
    assert "timed out" in result.error.lower() or "timeout" in result.error.lower()


from alertmux.schema import NormalisedAlert  # noqa: E402


def _feature(**prop_overrides) -> dict:
    props = {
        "mag": 2.27,
        "place": "6 km SE of Chickasha, Oklahoma",
        "time": 1787009589944,
        "url": "https://earthquake.usgs.gov/earthquakes/eventpage/ok1",
        "alert": None,
        "type": "earthquake",
        "title": "M 2.3 - somewhere",
    }
    props.update(prop_overrides)
    for key, value in list(props.items()):
        if value is _ABSENT:
            del props[key]
    return {
        "type": "Feature", "id": "ok1",
        "geometry": {"type": "Point", "coordinates": [0.0, 0.0, 0.0]},
        "properties": props,
    }


class _Absent:
    pass


_ABSENT = _Absent()


def _collection(*features) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def test_event_type_is_never_defaulted_to_earthquake():
    """The feed also emits quarry blast, explosion, ice quake and sonic
    boom. Defaulting would assert a quarry blast was an earthquake.
    D3: a missing type is a malformed record and is quarantined, not
    allowed to abort the whole fetch."""
    alerts = UsgsAdapter().parse(_collection(_feature(type=_ABSENT)), NOW)
    assert alerts == []
    assert alerts.invalid_count == 1
    assert any("no type" in sample for sample in alerts.invalid_samples)

    alerts = UsgsAdapter().parse(_collection(_feature(type="")), NOW)
    assert alerts == []
    assert alerts.invalid_count == 1
    assert any("no type" in sample for sample in alerts.invalid_samples)


def test_one_malformed_feature_among_valid_ones_is_quarantined_not_fatal():
    good1 = _feature()
    bad = _feature(type=_ABSENT)
    good2 = dict(_feature(), id="ok2")
    alerts = UsgsAdapter().parse(_collection(good1, bad, good2), NOW)
    assert len(alerts) == 2
    assert alerts.invalid_count == 1


def test_all_features_malformed_yields_empty_list_and_full_invalid_count():
    bad1 = _feature(type=_ABSENT)
    bad2 = dict(_feature(type=_ABSENT), id="bad2")
    alerts = UsgsAdapter().parse(_collection(bad1, bad2), NOW)
    assert alerts == []
    assert alerts.invalid_count == 2


@respx.mock
def test_fetch_quarantines_one_bad_record_and_still_returns_the_rest():
    good1 = _feature()
    bad = _feature(type=_ABSENT)
    good2 = dict(_feature(), id="ok2")
    payload = _collection(good1, bad, good2)
    respx.get(UsgsAdapter.URL).mock(return_value=httpx.Response(200, json=payload))
    result = UsgsAdapter().fetch()
    assert result.ok is True
    assert len(result.alerts) == 2
    assert result.invalid_count == 1
    assert len(result.invalid_samples) == 1


def test_non_earthquake_event_type_passes_through_verbatim():
    alert = UsgsAdapter().parse(_collection(_feature(type="quarry blast")), NOW)[0]
    assert alert.event == "quarry blast"


def test_description_is_none_not_a_copy_of_the_headline():
    """USGS has no description field. None must mean the same thing here
    as it does for SWIC: the source supplied nothing."""
    alert = UsgsAdapter().parse(FIXTURE, NOW)[0]
    assert alert.description is None
    assert "description" in alert.unavailable_fields
    assert alert.headline == "M 4.9 - 79 km N of Ruteng, Indonesia"


def test_unavailable_fields_is_exhaustive_in_both_directions():
    optional = [
        name for name in NormalisedAlert.model_fields
        if name not in {"id", "event", "provenance", "unavailable_fields", "unmapped_fields"}
    ]
    payloads = [
        FIXTURE,
        # place is genuinely null for deep-ocean events; title and
        # geometry can also be absent.
        _collection(_feature(place=None, title=None)),
        _collection(dict(_feature(), geometry=None)),
        _collection(_feature(alert="green")),
    ]
    for payload in payloads:
        for alert in UsgsAdapter().parse(payload, NOW):
            for name in alert.unavailable_fields:
                assert getattr(alert, name) is None, name
            for name in alert.unmapped_fields:
                assert getattr(alert, name) is None, name
            for name in optional:
                if getattr(alert, name) is None:
                    assert (
                        name in alert.unavailable_fields or name in alert.unmapped_fields
                    ), name


def test_unavailable_and_unmapped_never_overlap():
    payloads = [FIXTURE, _collection(_feature(alert="green"))]
    for payload in payloads:
        for alert in UsgsAdapter().parse(payload, NOW):
            overlap = set(alert.unavailable_fields) & set(alert.unmapped_fields)
            assert overlap == set(), overlap


def test_empty_feature_list_is_zero_alerts_not_an_error():
    assert UsgsAdapter().parse(_collection(), NOW) == []


def test_non_featurecollection_200_raises_rather_than_reporting_zero_quakes():
    with pytest.raises(ValueError, match="FeatureCollection"):
        UsgsAdapter().parse({"exceptions": [{"exceptionCode": "X"}]}, NOW)


@respx.mock
def test_fetch_reports_non_geojson_200_as_a_failure():
    respx.get(UsgsAdapter.URL).mock(
        return_value=httpx.Response(
            200, json={"exceptions": [{"exceptionCode": "InvalidParameterValue"}]}
        )
    )
    result = UsgsAdapter().fetch()
    assert result.ok is False
    assert result.error is not None
    assert result.alerts == []
