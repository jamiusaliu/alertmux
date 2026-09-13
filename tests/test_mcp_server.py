import asyncio
from datetime import datetime, timezone

import pytest

pytest.importorskip("mcp")

from alertmux.adapters.base import FetchResult
from alertmux.api import clear_cache
from alertmux.schema import NormalisedAlert, Provenance

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _alert(severity=None):
    return NormalisedAlert(
        id="wmo-swic:1",
        event="THUNDERSTORMS",
        area_description="Some states in Nigeria will be affected.",
        source_severity="3",
        severity=severity,
        provenance=Provenance(
            authority="ng-nimet",
            source_id="wmo-swic",
            source_url="https://severeweather.wmo.int/g/wfs",
            retrieved_at=NOW,
        ),
        unavailable_fields=[] if severity else ["severity"],
    )


class FakeAdapter:
    def __init__(self, source_id, ok=True, alerts=None, error=None):
        self.source_id = source_id
        self._result = FetchResult(
            source_id=source_id, ok=ok, alerts=alerts or [],
            error=error, retrieved_at=NOW, latency_ms=5,
        )
        self.URL = f"https://example.test/{source_id}"

    def fetch(self):
        return self._result


def setup_function():
    clear_cache()


def teardown_function():
    clear_cache()


def _run(coro):
    return asyncio.run(coro)


def _patch_adapters(monkeypatch, mcp_server, adapters):
    monkeypatch.setattr(mcp_server, "default_adapters", lambda: adapters)


def _call(server, name, arguments=None):
    result = _run(server.call_tool(name, arguments or {}))
    assert result.is_error is False
    return result.structured_content


def test_server_registers_exactly_four_tools():
    from alertmux.mcp_server import build_server

    server = build_server()
    tools = _run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "list_alerts",
        "list_sources",
        "get_hazard_coverage",
        "find_duplicates",
    }


def test_list_alerts_returns_alerts_partial_and_source_statuses(monkeypatch):
    from alertmux import mcp_server

    _patch_adapters(
        monkeypatch, mcp_server, [FakeAdapter("wmo-swic", alerts=[_alert()])]
    )
    server = mcp_server.build_server()
    body = _call(server, "list_alerts")

    assert body["alerts"][0]["event"] == "THUNDERSTORMS"
    assert body["partial"] is False
    assert body["sources"][0]["source_id"] == "wmo-swic"


def test_list_alerts_surfaces_unmapped_fields(monkeypatch):
    """An MCP consumer is an LLM that paraphrases the result -- if a
    declined-but-supplied value collapses into the same list as a
    genuinely missing one, the model has no way to state the
    distinction (DECISIONS.md D14's reasoning, extended to this field).
    """
    from alertmux import mcp_server

    unmapped = _alert(severity=None).model_copy(
        update={"unavailable_fields": [], "unmapped_fields": ["severity"]}
    )
    _patch_adapters(
        monkeypatch, mcp_server, [FakeAdapter("wmo-swic", alerts=[unmapped])]
    )
    server = mcp_server.build_server()
    body = _call(server, "list_alerts")

    assert body["alerts"][0]["unmapped_fields"] == ["severity"]
    assert "severity" not in body["alerts"][0]["unavailable_fields"]


def test_list_alerts_filters_by_authority(monkeypatch):
    from alertmux import mcp_server

    _patch_adapters(
        monkeypatch, mcp_server, [FakeAdapter("wmo-swic", alerts=[_alert()])]
    )
    server = mcp_server.build_server()

    body = _call(server, "list_alerts", {"authority": "ng-nimet"})
    assert len(body["alerts"]) == 1

    body_unknown = _call(server, "list_alerts", {"authority": "does-not-exist"})
    assert body_unknown["alerts"] == []
    assert body_unknown["available_authorities"] == ["ng-nimet"]


def test_unknown_authority_flags_a_partial_available_authorities_list(monkeypatch):
    """#24: over MCP the consumer paraphrases, so a short list must say so.

    A caller asks about an authority whose source happens to be down. The
    fetch is partial, the authority is genuinely real, and it is missing from
    `available_authorities` purely because its source failed. Without the flag
    that response is indistinguishable from "no such authority" -- which is
    the false negative #9 fixed for `/alerts`.
    """
    from alertmux import mcp_server

    _patch_adapters(
        monkeypatch,
        mcp_server,
        [
            FakeAdapter("wmo-swic", alerts=[_alert()]),
            FakeAdapter("usgs", ok=False, error="timeout"),
        ],
    )
    server = mcp_server.build_server()

    body = _call(server, "list_alerts", {"authority": "us-nws"})

    assert body["alerts"] == []
    # us-nws is real, but its source timed out, so it cannot appear here.
    assert body["available_authorities"] == ["ng-nimet"]
    assert body["partial"] is True
    assert body["available_authorities_partial"] is True


def test_available_authorities_partial_is_false_on_a_complete_fetch(monkeypatch):
    """The flag has to be able to say "no", or it carries no information.

    Every source answered, so a missing authority really is absent and the
    caller may say so.
    """
    from alertmux import mcp_server

    _patch_adapters(
        monkeypatch, mcp_server, [FakeAdapter("wmo-swic", alerts=[_alert()])]
    )
    server = mcp_server.build_server()

    body = _call(server, "list_alerts", {"authority": "does-not-exist"})

    assert body["available_authorities"] == ["ng-nimet"]
    assert body["partial"] is False
    assert body["available_authorities_partial"] is False


def test_available_authorities_partial_is_absent_when_the_list_is(monkeypatch):
    """No list to qualify means no flag -- not a bare `false`.

    A `false` next to a null list would read as "the authority list is
    complete", which is a claim about a list that was never built.
    """
    from alertmux import mcp_server

    _patch_adapters(
        monkeypatch,
        mcp_server,
        [
            FakeAdapter("wmo-swic", alerts=[_alert()]),
            FakeAdapter("usgs", ok=False, error="timeout"),
        ],
    )
    server = mcp_server.build_server()

    # A matching authority: no ambiguity to resolve, so neither field applies.
    body = _call(server, "list_alerts", {"authority": "ng-nimet"})

    assert len(body["alerts"]) == 1
    assert body["available_authorities"] is None
    assert body["available_authorities_partial"] is None


def test_list_alerts_description_tells_the_model_to_check_the_flag():
    """The description is what a model reads when deciding how to interpret
    the result, so the flag has to be named there too (#24)."""
    from alertmux import mcp_server

    server = mcp_server.build_server()
    tools = {t.name: t for t in _run(server.list_tools())}

    assert "available_authorities_partial" in tools["list_alerts"].description


def test_list_alerts_limit_truncation_is_flagged(monkeypatch):
    from alertmux import mcp_server

    alerts = []
    for i in range(5):
        a = _alert()
        a = a.model_copy(update={"id": f"wmo-swic:{i}"})
        alerts.append(a)

    _patch_adapters(monkeypatch, mcp_server, [FakeAdapter("wmo-swic", alerts=alerts)])
    server = mcp_server.build_server()

    body = _call(server, "list_alerts", {"limit": 2})
    assert len(body["alerts"]) == 2
    assert body["truncated"] is True
    assert body["total_matched"] == 5
    assert body["returned"] == 2

    body_full = _call(server, "list_alerts", {"limit": 50})
    assert body_full["truncated"] is False


def test_every_tool_response_contains_disclaimer(monkeypatch):
    from alertmux import mcp_server

    _patch_adapters(
        monkeypatch, mcp_server, [FakeAdapter("wmo-swic", alerts=[_alert()])]
    )
    server = mcp_server.build_server()

    for tool_name in (
        "list_alerts",
        "list_sources",
        "get_hazard_coverage",
        "find_duplicates",
    ):
        body = _call(server, tool_name)
        assert "not a substitute" in body["disclaimer"].lower()


def test_get_hazard_coverage_reports_uncovered_family(monkeypatch):
    from alertmux import mcp_server

    quake = _alert()
    quake = quake.model_copy(update={"event": "earthquake"})
    _patch_adapters(monkeypatch, mcp_server, [FakeAdapter("wmo-swic", alerts=[quake])])
    server = mcp_server.build_server()

    body = _call(server, "get_hazard_coverage")
    assert "tsunami" in body["uncovered_hazards"]
    assert body["hazard_coverage"]["earthquake"] == ["wmo-swic"]


def test_a_failing_adapter_produces_partial_true(monkeypatch):
    from alertmux import mcp_server

    _patch_adapters(
        monkeypatch,
        mcp_server,
        [
            FakeAdapter("wmo-swic", alerts=[_alert()]),
            FakeAdapter("usgs", ok=False, error="timeout"),
        ],
    )
    server = mcp_server.build_server()

    body = _call(server, "list_alerts")
    assert body["partial"] is True

    sources_body = _call(server, "list_sources")
    assert any(not s["ok"] for s in sources_body["sources"])


def test_list_alerts_known_authority_nonmatching_severity_is_not_unknown(monkeypatch):
    """A valid authority with no alerts at the requested severity must
    not take the unknown-authority branch. available_authorities is
    only populated when the authority itself is absent from the fetch.
    """
    from alertmux import mcp_server

    _patch_adapters(
        monkeypatch,
        mcp_server,
        [FakeAdapter("wmo-swic", alerts=[_alert(severity="Severe")])],
    )
    server = mcp_server.build_server()
    body = _call(
        server,
        "list_alerts",
        {"authority": "ng-nimet", "severity": "Extreme"},
    )

    assert body["alerts"] == []
    assert body["available_authorities"] is None
    assert body["total_matched"] == 0
