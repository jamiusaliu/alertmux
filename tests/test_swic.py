import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from alertmux.adapters.swic import SwicAdapter
from alertmux.schema import NormalisedAlert

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "swic_effective.json").read_text()
)
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def test_params_always_request_the_effective_view():
    params = SwicAdapter().build_params(mem=None, max_features=3000)
    assert params["typeName"] == "local_postgis:effective_warning_view"
    assert params["request"] == "GetFeature"
    assert params["outputFormat"] == "json"


def test_params_always_send_maxfeatures():
    """Unfiltered SWIC queries exceed 24MB and time out.

    This only checks that maxFeatures is always present in the request -
    it does not, and cannot, prove the whole result set was retrieved.
    Truncation is caught by test_truncated_response_is_flagged below.
    """
    assert "maxFeatures" in SwicAdapter().build_params(mem=None, max_features=500)
    assert SwicAdapter().build_params(mem=None, max_features=500)["maxFeatures"] == 500


def test_params_filter_by_authority_when_mem_given():
    params = SwicAdapter().build_params(mem="075", max_features=3000)
    assert params["cql_filter"] == "mem='075'"


def test_params_omit_filter_when_no_mem():
    assert "cql_filter" not in SwicAdapter().build_params(mem=None, max_features=10)


def test_parse_returns_one_alert_per_feature():
    assert len(SwicAdapter().parse(FIXTURE, NOW)) == 3


def test_parse_derives_authority_from_capurl_prefix():
    alerts = SwicAdapter().parse(FIXTURE, NOW)
    assert alerts[0].provenance.authority == "ng-nimet"
    assert alerts[1].provenance.authority == "in-ndma"


def test_parse_keeps_capurl_as_raw_reference():
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.provenance.raw_reference.startswith("ng-nimet-en/")


def test_parse_maps_event_and_area_verbatim():
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.event == "THUNDERSTORMS"
    assert alert.area_description == "Some states in Nigeria will be affected."


def test_parse_reads_sent_as_utc():
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.sent == datetime(2026, 8, 17, 6, 50, 16, tzinfo=timezone.utc)


def test_verified_codes_map_to_cap_names():
    """Confirmed against raw CAP: s=3 Severe, u=3 Expected, c=4 Observed."""
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.severity == "Severe"
    assert alert.urgency == "Expected"
    assert alert.certainty == "Observed"


def test_raw_codes_are_always_preserved_even_when_mapped():
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.source_severity == "3"
    assert alert.source_urgency == "3"
    assert alert.source_certainty == "4"


def test_extreme_code_maps():
    alert = SwicAdapter().parse(FIXTURE, NOW)[2]
    assert alert.source_severity == "4"
    assert alert.severity == "Extreme"
    assert alert.urgency == "Immediate"


def test_unverified_codes_stay_untranslated():
    """Codes 0, and 1 for urgency/certainty, were never observed.

    Guessing them would risk a missed warning or a false alarm.
    """
    payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "id": "x", "geometry": None,
            "properties": {
                "capurl": "xx-test-en/2026/08/17/00/00/00-a.xml",
                "sent": "2026-08-17T00:00:00Z", "event": "TEST",
                "s": 0, "u": 1, "c": 1, "mem": "999",
                "areadesc": "nowhere", "rlink": "",
            },
        }],
    }
    alert = SwicAdapter().parse(payload, NOW)[0]
    assert alert.source_severity == "0"
    assert alert.source_urgency == "1"
    assert alert.source_certainty == "1"
    assert alert.severity is None
    assert alert.urgency is None
    assert alert.certainty is None
    assert "severity" in alert.unavailable_fields
    assert "urgency" in alert.unavailable_fields
    assert "certainty" in alert.unavailable_fields


def test_null_geometry_is_recorded_as_unavailable():
    alert = SwicAdapter().parse(FIXTURE, NOW)[0]
    assert alert.geometry is None
    assert "geometry" in alert.unavailable_fields


def _feature(fid: str, capurl: str, **prop_overrides) -> dict:
    props = {
        "capurl": capurl,
        "sent": "2026-08-17T00:00:00Z",
        "event": "TEST",
        "s": 3, "u": 3, "c": 4, "mem": "999",
        "areadesc": "nowhere", "rlink": "",
    }
    props.update(prop_overrides)
    if capurl is None:
        props.pop("capurl")
    return {"type": "Feature", "id": fid, "geometry": None, "properties": props}


def _collection(*features) -> dict:
    return {
        "type": "FeatureCollection",
        "numberMatched": len(features),
        "numberReturned": len(features),
        "features": list(features),
    }


def test_id_is_derived_from_capurl_not_the_geoserver_fid():
    """GeoServer synthetic fids embed a REQUEST timestamp and change on
    every fetch. The same alert must keep the same id, or the notifier's
    dedupe key breaks and every poll re-notifies.
    """
    capurl = "ng-nimet-en/2026/08/17/14/50/16-e28162fa92b40b8a59c979ba00b562e9.xml"
    first = SwicAdapter().parse(
        _collection(_feature("effective_warning_view.fid--7463f54d_x_2d5b", capurl)), NOW
    )[0]
    second = SwicAdapter().parse(
        _collection(_feature("effective_warning_view.fid--7463f54d_x_2d5c", capurl)), NOW
    )[0]
    assert first.id == second.id
    assert first.id == f"wmo-swic:{capurl}"


def test_feature_without_capurl_raises_rather_than_using_the_fid():
    payload = _collection(_feature("effective_warning_view.fid--abc", None))
    with pytest.raises(ValueError, match="capurl"):
        SwicAdapter().parse(payload, NOW)


def test_unparseable_capurl_raises_rather_than_guessing_authority():
    payload = _collection(_feature("f1", "NOT-A-CAP-PATH/2026/08/17/x.xml"))
    with pytest.raises(ValueError, match="authority"):
        SwicAdapter().parse(payload, NOW)


def test_naive_sent_timestamp_raises_rather_than_assuming_local_time():
    """astimezone() on a naive value silently applies the SERVER's offset,
    shifting a hazard timestamp differently on every machine."""
    from alertmux.adapters.swic import _iso_utc

    with pytest.raises(ValueError, match="offset"):
        _iso_utc("2026-08-17T06:50:16", "sent")
    assert _iso_utc("2026-08-17T06:50:16Z", "sent") == datetime(
        2026, 8, 17, 6, 50, 16, tzinfo=timezone.utc
    )


def test_headline_and_description_are_never_fabricated():
    """The WFS list view carries neither. `rlink` points at a RELATED CAP
    file, so surfacing it as a description would relay a filename."""
    alerts = SwicAdapter().parse(FIXTURE, NOW)
    for alert in alerts:
        assert alert.headline is None
        assert alert.description is None
        assert "headline" in alert.unavailable_fields
        assert "description" in alert.unavailable_fields

    with_rlink = SwicAdapter().parse(
        _collection(_feature("f1", "xx-test-en/a.xml", rlink="xx-test-en/other.xml")),
        NOW,
    )[0]
    assert with_rlink.description is None


def test_unavailable_fields_is_exhaustive_in_both_directions():
    payloads = [FIXTURE, _collection(_feature("f1", "xx-test-en/a.xml", areadesc=None))]
    optional = [
        name for name in NormalisedAlert.model_fields
        if name not in {"id", "event", "provenance", "unavailable_fields"}
    ]
    for payload in payloads:
        for alert in SwicAdapter().parse(payload, NOW):
            for name in alert.unavailable_fields:
                assert getattr(alert, name) is None, name
            for name in optional:
                if getattr(alert, name) is None:
                    assert name in alert.unavailable_fields, name


def test_empty_feature_list_is_zero_alerts_not_an_error():
    """A legitimately quiet feed must stay distinguishable from the
    non-GeoJSON-200 case below."""
    assert SwicAdapter().parse(_collection(), NOW) == []


def test_non_featurecollection_200_raises_rather_than_reporting_zero_hazards():
    exception_report = {"exceptions": [{"exceptionCode": "InvalidParameterValue"}]}
    with pytest.raises(ValueError, match="FeatureCollection"):
        SwicAdapter().parse(exception_report, NOW)


@respx.mock
def test_fetch_reports_ows_exception_at_200_as_a_failure():
    """GeoServer answers a bad cql_filter with HTTP 200 and no features.
    Parsing that as 0 alerts would report 'no hazards worldwide, healthy'.
    """
    respx.get(SwicAdapter.URL).mock(
        return_value=httpx.Response(
            200, json={"exceptions": [{"exceptionCode": "InvalidParameterValue"}]}
        )
    )
    result = SwicAdapter().fetch()
    assert result.ok is False
    assert result.error is not None
    assert result.alerts == []


@respx.mock
def test_truncated_response_is_flagged_while_staying_ok():
    payload = dict(FIXTURE, numberMatched=2133, numberReturned=3)
    respx.get(SwicAdapter.URL).mock(return_value=httpx.Response(200, json=payload))
    result = SwicAdapter(max_features=3).fetch()
    assert result.ok is True
    assert result.truncated is True
    assert result.matched == 2133
    assert result.returned == 3


@respx.mock
def test_complete_response_is_not_flagged_as_truncated():
    respx.get(SwicAdapter.URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    assert SwicAdapter().fetch().truncated is False


@respx.mock
def test_fetch_returns_ok_result():
    respx.get(SwicAdapter.URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    result = SwicAdapter().fetch()
    assert result.ok is True
    assert result.source_id == "wmo-swic"
    assert len(result.alerts) == 3


@respx.mock
def test_fetch_passes_mem_filter_to_the_request():
    route = respx.route(url__startswith=SwicAdapter.URL).mock(
        return_value=httpx.Response(200, json=FIXTURE)
    )
    SwicAdapter(mem="075").fetch()
    assert "mem%3D%27075%27" in str(route.calls[0].request.url) or (
        "mem='075'" in str(route.calls[0].request.url)
    )


@respx.mock
def test_fetch_reports_error_without_raising():
    respx.get(SwicAdapter.URL).mock(return_value=httpx.Response(500))
    result = SwicAdapter().fetch()
    assert result.ok is False
    assert result.alerts == []
    assert "500" in result.error


def test_feature_without_properties_fails_loudly():
    bad = {"type": "FeatureCollection", "features": [{"type": "Feature", "id": "f1"}]}
    with pytest.raises(ValueError, match="no properties"):
        SwicAdapter().parse(bad, NOW)


def test_feature_without_event_fails_loudly():
    bad = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "id": "f1", "geometry": None,
            "properties": {"capurl": "xx-test-en/a.xml", "sent": "2026-08-17T00:00:00Z"},
        }],
    }
    with pytest.raises(ValueError, match="no event"):
        SwicAdapter().parse(bad, NOW)


def test_severity_codes_as_digit_strings_still_map():
    """Some SWIC authorities emit s/u/c as "3" instead of 3. A string key
    against the int table used to miss silently and leave the named field
    null. Digit-string codes must map to the same CAP names as their int
    twins, with the raw string preserved in source_severity/urgency/certainty.
    """
    alert = SwicAdapter().parse(
        _collection(_feature("f1", "xx-test-en/a.xml", s="3", u="3", c="4")),
        NOW,
    )[0]
    assert alert.severity == "Severe"
    assert alert.urgency == "Expected"
    assert alert.certainty == "Observed"
    assert alert.source_severity == "3"
    assert alert.source_urgency == "3"
    assert alert.source_certainty == "4"
    assert "severity" not in alert.unavailable_fields
    assert "urgency" not in alert.unavailable_fields
    assert "certainty" not in alert.unavailable_fields


def test_non_digit_string_code_stays_unmapped():
    """A string that is not a digit (e.g. free-text severity) must not be
    forced through the table; it stays unmapped and is recorded as
    unavailable, exactly as an unverified int code would. Int codes on
    the same feature are unaffected and still map.
    """
    alert = SwicAdapter().parse(
        _collection(_feature("f1", "xx-test-en/a.xml", s="high", u=3, c=4)),
        NOW,
    )[0]
    assert alert.severity is None
    assert alert.source_severity == "high"
    assert "severity" in alert.unavailable_fields
    assert alert.urgency == "Expected"
    assert alert.certainty == "Observed"
