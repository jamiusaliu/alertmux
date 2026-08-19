"""HTTP presentation of the query layer.

This service relays alerts published by official authorities. It never
originates, edits, or rewords hazard content, and it is not a substitute
for official warnings.
"""

from __future__ import annotations

import threading
import time

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from alertmux.adapters import default_adapters
from alertmux.adapters.swic import CapDetailError, enrich_with_detail
from alertmux.query import AlertsResponse, collect
from alertmux.registry import (
    AuthoritiesResponse,
    RegisterUnavailable,
    build_authorities_response,
    get_register,
)
from alertmux.schema import DISCLAIMER, NormalisedAlert
from alertmux.sources import SourcesResponse, build_sources_response

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

# A degraded fetch is cached too, or a flapping source would defeat the
# cache entirely. But holding it for the full minute pins the failure:
# /health keeps answering 503, and /alerts keeps omitting a source, for up
# to 60s after that source has recovered. Re-check a partial result sooner.
PARTIAL_CACHE_TTL_SECONDS = 10.0

_cache_lock = threading.Lock()
_cache: tuple[float, AlertsResponse] | None = None


def clear_cache() -> None:
    """Drop the cached fetch. Used by tests; harmless in production."""
    global _cache
    with _cache_lock:
        _cache = None


def _ttl_for(response: AlertsResponse) -> float:
    """How long this response stays fresh.

    A complete fetch is good for the full TTL. A degraded one is re-checked
    sooner, so recovery becomes visible in seconds rather than a minute.
    """
    return PARTIAL_CACHE_TTL_SECONDS if response.partial else CACHE_TTL_SECONDS


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
        if cached is not None and (time.monotonic() - cached[0]) < _ttl_for(cached[1]):
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

    An unknown `?authority=` reports `available_authorities` (D7) and, in
    the same response, `available_authorities_partial`: `true` means that
    list was built from a partial fetch, so an authority missing from it
    may simply have a source that was down, not one that does not exist.
    Check `available_authorities_partial` before reading a short list as
    proof an authority is invalid.
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
            # D7, extended: the list above is built only from alerts this
            # fetch actually returned. If the fetch was partial (a source
            # was down, truncated, or quarantined records), a perfectly
            # valid authority whose source failed is silently missing
            # from it -- indistinguishable, without this flag, from an
            # authority that does not exist. Never let a short list read
            # as authoritative.
            response.available_authorities_partial = response.partial
        response.alerts = matching
    return response


@app.get("/alerts/{alert_id:path}/detail")
def alert_detail(alert_id: str, adapters=Depends(get_adapters)) -> NormalisedAlert:
    """Fetch one alert's raw CAP file and merge it in (issue #4).

    SWIC's list view -- what `/alerts` serves -- structurally omits
    `headline`, `description`, `instruction`, `onset` and `expires`;
    `expires` matters most, since without it a future notifier cannot
    tell a live warning from a lapsed one. Those fields live only in the
    authority's original CAP file, which this route fetches on request
    and merges into the alert already known from the current cached
    fetch (see `alertmux.adapters.swic.enrich_with_detail`).

    Deliberately **not** part of `/alerts`: with ~2,200 alerts in force,
    fetching one CAP file per alert on every list poll would be ~2,200
    requests to WMO per fetch. This route costs exactly one extra
    request, per alert, on demand -- and that request is cached by
    `capurl` forever afterwards, since a CAP file is immutable once
    published (its path is content-addressed).

    Two distinct failure modes, two distinct status codes: `alert_id`
    not present in the current fetch is `404` (an unknown id, not an
    empty detail); the id is known but its CAP file could not be
    fetched or parsed is `502` (the *alert* is real, the *detail
    request* to WMO failed). Neither case returns an empty or
    partially-blank record that would read as "no detail exists" --
    principle 4 applies here exactly as everywhere else in this project.

    Only alerts sourced from `wmo-swic` carry a `raw_reference` shaped
    like a SWIC `capurl`; an alert from any other source (or a future
    SWIC record with `raw_reference` unset) has no CAP file this route
    knows how to resolve, and is reported as `404` rather than silently
    returning the un-enriched alert.

    `{alert_id:path}` (not the plain `{alert_id}` string converter): an
    alert id is `f"{source_id}:{capurl}"` and a `capurl` itself contains
    `/` (e.g. `ng-nimet-en/2026/08/17/14/50/16-<hash>.xml`). The default
    path converter stops at the first `/`, which would make most SWIC
    ids unroutable.
    """
    result = _collect_shared(adapters)
    alert = next((a for a in result.alerts if a.id == alert_id), None)
    if alert is None:
        raise HTTPException(
            status_code=404, detail=f"No alert with id {alert_id!r} in the current fetch."
        )

    swic_adapter = next(
        (
            a
            for a in adapters
            if getattr(a, "source_id", None) == "wmo-swic" and hasattr(a, "fetch_detail")
        ),
        None,
    )
    if (
        swic_adapter is None
        or alert.provenance.source_id != "wmo-swic"
        or not alert.provenance.raw_reference
    ):
        raise HTTPException(
            status_code=404,
            detail=(
                f"No CAP detail is available for source "
                f"{alert.provenance.source_id!r}."
            ),
        )

    try:
        detail = swic_adapter.fetch_detail(alert.provenance.raw_reference)
    except CapDetailError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return enrich_with_detail(alert, detail)


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


@app.get("/sources")
def sources(adapters=Depends(get_adapters)) -> SourcesResponse:
    """What this system covers, and what it misses.

    Where `/health` answers "is it working," this answers "what does
    alertmux actually cover, and what does it miss" -- per-source
    identity and structural shape (`structural_gaps`, from each
    adapter's `STRUCTURAL_GAPS`, reportable without a fetch), the
    authorities actually observed this fetch (not a declared list), and
    `hazard_coverage`: which sources returned at least one alert in each
    hazard family. `uncovered_hazards` names families with zero
    contributing sources -- an authority count alone can look healthy
    (e.g. 59/300 sources) while a whole hazard family, such as tsunami,
    has no coverage at all. That is this endpoint's reason to exist.

    `hazard_coverage`/`uncovered_hazards` classify each alert's
    free-text `event` field with an explicit keyword table
    (`sources.HAZARD_KEYWORDS`) -- heuristic, not authoritative. It will
    misfile some alerts (wording varies by authority) and it never
    alters `NormalisedAlert` data anywhere else; see `sources.py`'s
    module docstring for the full caveat.

    Read-only: takes the shared cached object, like `/health`, per D9.
    """
    result = _collect_shared(adapters)
    return build_sources_response(adapters, result)


@app.get("/authorities")
def authorities(
    country: str | None = None, adapters=Depends(get_adapters)
) -> AuthoritiesResponse:
    """The WMO Register of Alerting Authorities -- a coverage map, not a
    source of alerts.

    This lists official alerting authorities and the CAP categories
    each covers; it never carries a warning itself. `?country=` accepts
    either an alpha-2 (`NG`) or alpha-3 (`NGA`) code.

    `countries_covered`/`countries_uncovered` are joined at country
    level only, never authority level -- WMO's own authority
    abbreviations (`raa:authorityAbbrev`) disagree with the ones this
    project's sources use (Nigeria: WMO's `nma` vs SWIC's `nimet`), so
    no cross-source authority match can be asserted in general. See
    `registry.py`'s module docstring and DECISIONS.md for the full
    reasoning. Each entry's `matched_authority` is set only where an
    exact reconstruction happens to hold (e.g. `us-noaa`) -- everything
    else is `null`, meaning *unmatched*, which is a distinct claim from
    "uncovered": an authority alertmux cannot prove a link for is not
    the same as a country nobody warns for.

    The register is cached with a long TTL (it changes rarely) and
    `register_cache_age_seconds` always reports how old the served copy
    is. If the live fetch fails, the last cache is served with
    `register_fetch_error` set; with no cache at all, this returns a
    clear 503 error rather than an empty authorities list.
    """
    try:
        register_authorities, fetched_at, age, fetch_error = get_register()
    except RegisterUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={"error": str(exc), "disclaimer": DISCLAIMER},
        )

    # Coverage is measured, never declared: same cached, read-only
    # collect path /sources uses (D9).
    result = _collect_shared(adapters)
    return build_authorities_response(
        register_authorities,
        fetched_at,
        age,
        fetch_error,
        result,
        country=country,
    )
