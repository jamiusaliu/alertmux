"""HTTP presentation of the query layer.

This service relays alerts published by official authorities. It never
originates, edits, or rewords hazard content, and it is not a substitute
for official warnings.
"""

from __future__ import annotations

import threading
import time

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from alertmux.adapters import default_adapters
from alertmux.query import AlertsResponse, collect
from alertmux.schema import DISCLAIMER

__all__ = ["app", "get_adapters", "clear_cache", "DISCLAIMER"]

app = FastAPI(
    title="alertmux",
    description=DISCLAIMER,
    version="0.1.0",
)

# An unfiltered SWIC fetch is ~774KB. /health used to trigger one per
# call, so a monitor polling every 30s hammered WMO and USGS. A whole-
# result TTL cache in front of collect() fixes that without a dependency.
CACHE_TTL_SECONDS = 60.0
_cache_lock = threading.Lock()
_cache: tuple[float, AlertsResponse] | None = None


def clear_cache() -> None:
    """Drop the cached fetch. Used by tests; harmless in production."""
    global _cache
    with _cache_lock:
        _cache = None


def _collect_shared(adapters) -> AlertsResponse:
    """collect() behind the TTL cache, returning the *shared* cached object.

    The caller must treat the result as read-only, including everything
    reachable from it. Anything that mutates the response -- notably the
    in-place `alerts` filter in /alerts -- must go through
    `_collect_cached()` instead, per D9.
    """
    global _cache
    with _cache_lock:
        cached = _cache
        if cached is not None and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS:
            return cached[1]

    response = collect(adapters)

    with _cache_lock:
        _cache = (time.monotonic(), response)
    return response


def _collect_cached(adapters) -> AlertsResponse:
    """collect() behind a TTL cache, returning a private copy each time.

    The copy matters: callers filter `alerts` in place, and the cached
    object must not be mutated by one request on behalf of the next.
    """
    return _collect_shared(adapters).model_copy(deep=True)


def get_adapters():
    """Overridable in tests via app.dependency_overrides."""
    return default_adapters()


@app.get("/alerts")
def alerts(
    authority: str | None = None, adapters=Depends(get_adapters)
) -> AlertsResponse:
    """All alerts currently in force, optionally filtered by authority.

    Note: `sources[].alert_count` describes the whole fetch, not the
    filtered list. With `?authority=` applied it will not equal
    `len(alerts)` — source health is about the fetch, not the filter.
    """
    response = _collect_cached(adapters)
    if authority:
        matching = [
            a for a in response.alerts if a.provenance.authority == authority
        ]
        if not matching:
            # An unknown authority and a quiet day both return zero
            # alerts. In this domain that ambiguity is a dangerous false
            # negative, so name the authorities this fetch actually saw.
            response.available_authorities = sorted(
                {a.provenance.authority for a in response.alerts}
            )
        response.alerts = matching
    return response


@app.get("/health")
def health(adapters=Depends(get_adapters)) -> dict:
    # Read-only: /health reports source status and never touches `alerts`,
    # so it takes the shared object rather than paying the deep copy D9
    # requires for the in-place filter in /alerts. model_dump() below
    # already builds fresh dicts, so nothing aliased escapes this handler.
    result = _collect_shared(adapters)
    body = {
        "ok": not result.partial,
        "checked_at": result.retrieved_at.isoformat(),
        "sources": [s.model_dump() for s in result.sources],
        "disclaimer": DISCLAIMER,
    }
    if result.partial:
        # HTTP 200 with {"ok": false} reads green to every standard
        # monitor. Degraded service must be visible at the status line.
        return JSONResponse(status_code=503, content=body)
    return body
