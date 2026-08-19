"""The operational dashboard: its own FastAPI app, separate from api.py.

Local, read-only, single operator. Every route here is a GET; there is
no route that could originate, edit, or suppress an alert (see
docs/DECISIONS.md for why this is a hard separation from `api.py`, not
a router mounted onto it).

Serves one self-contained HTML page (`GET /`, inline CSS/JS, no CDN, no
build step -- see `dashboard/page.py`) plus the JSON endpoints that page
polls. Every endpoint reads from the same TTL-cached collect path
(`dashboard/collector.py`); nothing here issues a second fetch against
WMO, USGS, or any other upstream per request.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from alertmux.adapters import default_adapters
from alertmux.dashboard.collector import DashboardCollector
from alertmux.dashboard.page import render_page
from alertmux.dashboard.volume import DEFAULT_VOLUME_PATH, VolumeRecorder
from alertmux.notify.runlog import DEFAULT_RUN_LOG_PATH, RunLogStore
from alertmux.query import AlertsResponse
from alertmux.registry import RegisterUnavailable, get_register
from alertmux.schema import DISCLAIMER
from alertmux.sources import HAZARD_KEYWORDS, build_sources_response, classify_hazard

__all__ = [
    "app",
    "get_adapters",
    "get_collector",
    "get_run_log",
    "clear_state",
    "DISCLAIMER",
]

HEURISTIC_CAVEAT = (
    "Hazard-family coverage below is heuristic: each alert's free-text "
    "'event' field is matched against a fixed keyword table, not an "
    "authoritative classification. It can misfile or fail to classify an "
    "event whose wording the table has not seen. It never alters alert "
    "data anywhere else -- see alertmux.sources for the full caveat."
)

app = FastAPI(
    title="alertmux dashboard",
    description=DISCLAIMER,
    version="0.1.0",
)

_volume_recorder = VolumeRecorder(DEFAULT_VOLUME_PATH)
_collector = DashboardCollector(volume_recorder=_volume_recorder)
_run_log = RunLogStore(DEFAULT_RUN_LOG_PATH)


def configure(*, volume_path: str | None = None, run_log_path: str | None = None) -> None:
    """Point the dashboard's persistence at non-default paths. Called by
    `dashboard/cli.py` before the server starts; also used by tests that
    want a real (tmp_path) file rather than the module default."""
    global _volume_recorder, _collector, _run_log
    if volume_path is not None:
        _volume_recorder = VolumeRecorder(volume_path)
        _collector = DashboardCollector(volume_recorder=_volume_recorder)
    if run_log_path is not None:
        _run_log = RunLogStore(run_log_path)


def get_adapters():
    """Overridable in tests via app.dependency_overrides, same pattern
    as api.py's get_adapters."""
    return default_adapters()


def get_collector() -> DashboardCollector:
    """Overridable in tests via app.dependency_overrides."""
    return _collector


def get_run_log() -> RunLogStore:
    """Overridable in tests via app.dependency_overrides."""
    return _run_log


def clear_state() -> None:
    """Reset cache and source-health memory. Used by tests; harmless in
    production."""
    _collector.clear()


def _source_health_payload(response: AlertsResponse, collector: DashboardCollector) -> list[dict]:
    health = collector.health_snapshot()
    payload = []
    for status in response.sources:
        entry = health.get(status.source_id)
        payload.append(
            {
                "source_id": status.source_id,
                "ok": status.ok,
                "error": status.error,
                "latency_ms": status.latency_ms,
                "alert_count": status.alert_count,
                "truncated": status.truncated,
                "invalid_count": status.invalid_count,
                "consecutive_failures": entry.consecutive_failures if entry else (
                    0 if status.ok else 1
                ),
                "last_ok_at": entry.last_ok_at.isoformat()
                if entry and entry.last_ok_at
                else None,
            }
        )
    return payload


def _registry_freshness() -> dict:
    try:
        _authorities, fetched_at, age, fetch_error = get_register()
    except RegisterUnavailable as exc:
        return {
            "available": False,
            "error": str(exc),
            "fetched_at": None,
            "cache_age_seconds": None,
            "fetch_error": None,
        }
    return {
        "available": True,
        "error": None,
        "fetched_at": fetched_at.isoformat(),
        "cache_age_seconds": age,
        "fetch_error": fetch_error,
    }


@app.get("/api/summary")
def api_summary(
    adapters=Depends(get_adapters),
    collector: DashboardCollector = Depends(get_collector),
    run_log: RunLogStore = Depends(get_run_log),
) -> JSONResponse:
    response = collector.collect(adapters)
    sources_response = build_sources_response(adapters, response)

    recorder = collector.volume_recorder
    volume_entries = recorder.read(limit=200) if recorder else []
    recording_started_at = recorder.recording_started_at() if recorder else None

    run_entries = run_log.read(limit=20)
    last_run = run_entries[-1] if run_entries else None

    body = {
        "retrieved_at": response.retrieved_at.isoformat(),
        "partial": response.partial,
        "disclaimer": DISCLAIMER,
        "heuristic_caveat": HEURISTIC_CAVEAT,
        "sources": _source_health_payload(response, collector),
        "hazard_coverage": sources_response.hazard_coverage,
        "uncovered_hazards": sources_response.uncovered_hazards,
        "all_hazard_families": sorted(HAZARD_KEYWORDS),
        "authorities_by_source": {
            s.source_id: s.authorities for s in sources_response.sources
        },
        "volume_history": volume_entries,
        "volume_recording_started_at": recording_started_at,
        "notification_runs": run_entries,
        "last_run_ok": last_run.get("ok") if last_run else None,
        "registry": _registry_freshness(),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    return JSONResponse(content=body)


@app.get("/api/alerts")
def api_alerts(
    authority: str | None = None,
    severity: str | None = None,
    event: str | None = None,
    adapters=Depends(get_adapters),
    collector: DashboardCollector = Depends(get_collector),
) -> JSONResponse:
    """Live alerts for the dashboard's demo panel, filterable by
    authority, severity and event substring. Read-only: filters a copy,
    never mutates the shared cached response."""
    response = collector.collect(adapters)
    alerts = list(response.alerts)

    if authority:
        alerts = [a for a in alerts if a.provenance.authority == authority]
    if severity:
        alerts = [a for a in alerts if (a.severity or "") == severity]
    if event:
        needle = event.casefold()
        alerts = [a for a in alerts if needle in (a.event or "").casefold()]

    payload = [
        {
            "id": a.id,
            "event": a.event,
            "headline": a.headline,
            "severity": a.severity,
            "authority": a.provenance.authority,
            "source_id": a.provenance.source_id,
            "area_description": a.area_description,
            "sent": a.sent.isoformat() if a.sent else None,
            "expires": a.expires.isoformat() if a.expires else None,
            "hazard_family": classify_hazard(a.event),
        }
        for a in alerts
    ]
    return JSONResponse(
        content={
            "alerts": payload,
            "count": len(payload),
            "partial": response.partial,
            "disclaimer": DISCLAIMER,
        }
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(content=render_page())
