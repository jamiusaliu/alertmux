"""Tests for the dashboard app -- everything renders from a fake collect
result, no network. Mirrors tests/test_sources.py's FakeAdapter pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from alertmux.adapters.base import FetchResult
from alertmux.dashboard.app import app, clear_state, get_adapters, get_collector, get_run_log
from alertmux.dashboard.collector import DashboardCollector
from alertmux.notify.runlog import RunLogStore
from alertmux.schema import DISCLAIMER, NormalisedAlert, Provenance

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
FROZEN_REGISTER = ([], NOW, 3600.0, None)


def _alert(alert_id, source_id, authority, event, severity=None):
    return NormalisedAlert(
        id=alert_id,
        event=event,
        severity=severity,
        provenance=Provenance(
            authority=authority,
            source_id=source_id,
            source_url="https://example.test",
            retrieved_at=NOW,
        ),
    )


class FakeAdapter:
    source_id = "fake"
    URL = "https://example.test/feed"
    STRUCTURAL_GAPS = ()

    def __init__(self, source_id, ok=True, alerts=None, error=None, truncated=False, invalid_count=0):
        self.source_id = source_id
        self._result = FetchResult(
            source_id=source_id,
            ok=ok,
            alerts=alerts or [],
            error=error,
            retrieved_at=NOW,
            latency_ms=5,
            truncated=truncated,
            invalid_count=invalid_count,
        )

    def fetch(self):
        return self._result


def _client(adapters, *, run_log_store=None, tmp_path=None):
    app.dependency_overrides[get_adapters] = lambda: adapters
    app.dependency_overrides[get_collector] = lambda: DashboardCollector()
    if run_log_store is not None:
        app.dependency_overrides[get_run_log] = lambda: run_log_store
    elif tmp_path is not None:
        app.dependency_overrides[get_run_log] = lambda: RunLogStore(tmp_path / "runs.jsonl")
    return TestClient(app)


def setup_function():
    clear_state()


def teardown_function():
    app.dependency_overrides.clear()
    clear_state()


def test_no_non_get_routes_exist():
    """Read-only, hard constraint: no route on this app may mutate
    anything -- assert every route only exposes GET."""
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods is None:
            continue
        assert methods <= {"GET", "HEAD"}, f"{route.path} exposes {methods}"


def test_index_page_contains_disclaimer_and_heuristic_caveat():
    client = _client([])
    resp = client.get("/")
    assert resp.status_code == 200
    assert DISCLAIMER in resp.text
    assert "heuristic" in resp.text.lower()


def test_summary_complete_fetch_is_not_partial(tmp_path):
    client = _client(
        [FakeAdapter("wmo-swic", alerts=[_alert("a:1", "wmo-swic", "ng-nimet", "Wildfire")])],
        tmp_path=tmp_path,
    )
    with patch("alertmux.dashboard.app.get_register", return_value=FROZEN_REGISTER):
        body = client.get("/api/summary").json()
    assert body["partial"] is False
    assert body["disclaimer"] == DISCLAIMER


def test_summary_partial_fetch_is_flagged(tmp_path):
    client = _client(
        [FakeAdapter("usgs", ok=False, error="timeout")],
        tmp_path=tmp_path,
    )
    with patch("alertmux.dashboard.app.get_register", return_value=FROZEN_REGISTER):
        body = client.get("/api/summary").json()
    assert body["partial"] is True


def test_source_health_lists_every_adapter_with_consecutive_failures(tmp_path):
    client = _client(
        [FakeAdapter("usgs", ok=False, error="timeout")],
        tmp_path=tmp_path,
    )
    with patch("alertmux.dashboard.app.get_register", return_value=FROZEN_REGISTER):
        body = client.get("/api/summary").json()
    usgs = next(s for s in body["sources"] if s["source_id"] == "usgs")
    assert usgs["ok"] is False
    assert usgs["consecutive_failures"] >= 1
    assert usgs["error"] == "timeout"


def test_family_with_no_sources_appears_in_uncovered_list(tmp_path):
    client = _client(
        [FakeAdapter("wmo-swic", alerts=[_alert("a:1", "wmo-swic", "ng-nimet", "Heat Advisory")])],
        tmp_path=tmp_path,
    )
    with patch("alertmux.dashboard.app.get_register", return_value=FROZEN_REGISTER):
        body = client.get("/api/summary").json()
    assert "tsunami" in body["uncovered_hazards"]
    assert "volcano" in body["uncovered_hazards"]
    assert "heat" not in body["uncovered_hazards"]


def test_live_alerts_filterable_by_authority_severity_event(tmp_path):
    adapters = [
        FakeAdapter(
            "wmo-swic",
            alerts=[
                _alert("a:1", "wmo-swic", "ng-nimet", "Flood Warning", severity="Severe"),
                _alert("a:2", "wmo-swic", "us-noaa", "Heat Advisory", severity="Moderate"),
            ],
        )
    ]
    client = _client(adapters, tmp_path=tmp_path)

    body = client.get("/api/alerts").json()
    assert body["count"] == 2

    body = client.get("/api/alerts", params={"authority": "ng-nimet"}).json()
    assert body["count"] == 1
    assert body["alerts"][0]["authority"] == "ng-nimet"

    body = client.get("/api/alerts", params={"severity": "Severe"}).json()
    assert body["count"] == 1

    body = client.get("/api/alerts", params={"event": "heat"}).json()
    assert body["count"] == 1
    assert body["alerts"][0]["event"] == "Heat Advisory"


def test_empty_volume_history_reports_no_data_yet(tmp_path):
    client = _client(
        [FakeAdapter("wmo-swic")],
        tmp_path=tmp_path,
    )
    with patch("alertmux.dashboard.app.get_register", return_value=FROZEN_REGISTER):
        body = client.get("/api/summary").json()
    # No recorder is wired to the ad hoc DashboardCollector() used by the
    # test client override, so history must never be reported as "zero
    # alerts" -- it must be reported as absent.
    assert body["volume_recording_started_at"] is None
    assert body["volume_history"] == []


def test_failed_notifier_run_appears_prominently(tmp_path):
    run_log = RunLogStore(tmp_path / "runs.jsonl")
    from alertmux.notify.runner import RunReport

    run_log.append(RunReport(sent_count=0, failures=["rule 'all' alert a1: SMTP timeout"]), when=NOW)

    client = _client([FakeAdapter("wmo-swic")], run_log_store=run_log)
    with patch("alertmux.dashboard.app.get_register", return_value=FROZEN_REGISTER):
        body = client.get("/api/summary").json()

    assert body["last_run_ok"] is False
    assert body["notification_runs"][-1]["failures"]


def test_clean_notifier_run_is_not_a_false_alarm(tmp_path):
    from alertmux.notify.runner import RunReport

    run_log = RunLogStore(tmp_path / "runs.jsonl")
    run_log.append(RunReport(sent_count=3, matched_by_rule={"all": 3}), when=NOW)

    client = _client([FakeAdapter("wmo-swic")], run_log_store=run_log)
    with patch("alertmux.dashboard.app.get_register", return_value=FROZEN_REGISTER):
        body = client.get("/api/summary").json()

    assert body["last_run_ok"] is True
    assert body["notification_runs"][-1]["failures"] == []


def test_no_notifier_runs_yet_is_not_reported_as_failed(tmp_path):
    run_log = RunLogStore(tmp_path / "runs.jsonl")
    client = _client([FakeAdapter("wmo-swic")], run_log_store=run_log)
    with patch("alertmux.dashboard.app.get_register", return_value=FROZEN_REGISTER):
        body = client.get("/api/summary").json()
    assert body["last_run_ok"] is None
    assert body["notification_runs"] == []
