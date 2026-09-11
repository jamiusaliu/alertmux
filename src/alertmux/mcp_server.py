"""MCP presentation of the query layer.

Exposes the same `collect()`-backed data the HTTP API serves, over MCP so a
model can query it from inside Claude. This module never fetches or
normalises anything itself -- it reuses `api._collect_cached()` (the same
TTL-cached path `/alerts`, `/health` and `/sources` already use) so an MCP
client never hammers WMO independently of the HTTP API. See D9 for why the
cache hands out deep copies.

This service relays alerts published by official authorities. It never
originates, edits, or rewords hazard content, and it is not a substitute
for official warnings -- see `alertmux.schema.DISCLAIMER`. That disclaimer
matters more here than over HTTP: the consumer of a tool result is an LLM
that will paraphrase it for an end user, so every response model below
carries the disclaimer as a field, not just as a docstring a model might
never read.

`mcp` is an optional dependency (`pip install alertmux[mcp]`) -- see
DECISIONS.md for why. This module is only importable when it is installed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from mcp.server.mcpserver import MCPServer

from alertmux.adapters import default_adapters
from alertmux.api import _collect_cached
from alertmux.query import SourceStatus
from alertmux.schema import DISCLAIMER, NormalisedAlert
from alertmux.sources import HAZARD_KEYWORDS, build_sources_response


class ListAlertsResult(BaseModel):
    """Result of `list_alerts`.

    `partial` and `sources` describe the underlying fetch, not the filtered
    `alerts` list -- the same distinction the HTTP `/alerts` endpoint makes
    (see DECISIONS.md D6). A model relaying this must check `partial`
    before claiming the answer is complete, and must check `truncated`
    before claiming `alerts` is the full matching set.
    """

    alerts: list[NormalisedAlert] = Field(default_factory=list)
    sources: list[SourceStatus] = Field(default_factory=list)
    partial: bool = False
    # True when more alerts matched the filters than `limit` allowed
    # through. Distinct from `partial`: this is honest truncation of a
    # complete answer, not an incomplete fetch.
    truncated: bool = False
    total_matched: int = 0
    returned: int = 0
    available_authorities: list[str] | None = None
    disclaimer: str = DISCLAIMER


class ListSourcesResult(BaseModel):
    """Result of `list_sources` -- same content as HTTP `GET /sources`."""

    sources: list[dict] = Field(default_factory=list)
    hazard_coverage: dict[str, list[str]] = Field(default_factory=dict)
    uncovered_hazards: list[str] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


class HazardCoverageResult(BaseModel):
    """Result of `get_hazard_coverage`.

    `uncovered_hazards` means *no source answers this hazard family*, not
    that the hazard is not occurring. Classification is heuristic
    (`sources.HAZARD_KEYWORDS`, matched against each alert's free-text
    `event` field) -- see `sources.py`'s module docstring for the caveat
    a consuming model should carry forward.
    """

    hazard_coverage: dict[str, list[str]] = Field(default_factory=dict)
    uncovered_hazards: list[str] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


class FindDuplicatesResult(BaseModel):
    """Result of `find_duplicates`. Reports only -- never merges or drops
    anything from `/alerts`/`list_alerts`. See DECISIONS.md D13."""

    duplicate_groups: list[dict] = Field(default_factory=list)
    ambiguous_duplicate_groups: int = 0
    disclaimer: str = DISCLAIMER


def build_server() -> MCPServer:
    server = MCPServer(
        name="alertmux",
        version="0.4.0",
        instructions=(
            "Query live natural-hazard alerts relayed from official "
            "alerting authorities (WMO SWIC, USGS, NWS, GDACS, NASA "
            "EONET). " + DISCLAIMER
        ),
    )

    @server.tool(
        description=(
            "Current natural-hazard alerts, optionally filtered by "
            "`authority` (the issuing authority id, e.g. 'ng-nimet') "
            "and/or `severity` (a named CAP level, e.g. 'Severe' -- "
            "only alerts whose source stated that level match; a null "
            "severity is never treated as a match). `limit` caps how "
            "many alerts are returned (default 50); if more matched, "
            "`truncated` is set on the response rather than silently "
            "dropping the rest. `partial` reflects whether every "
            "source answered this fetch -- if true, treat the alert "
            "list as possibly incomplete, not as 'no more alerts "
            "exist'. An unknown `authority` returns an empty list "
            "(never an error) and names the authorities that did "
            "answer in `available_authorities`, so a typo can be told "
            "apart from a genuinely quiet day."
        )
    )
    def list_alerts(
        authority: str | None = None,
        severity: str | None = None,
        limit: int = 50,
    ) -> ListAlertsResult:
        response = _collect_cached(default_adapters())

        # Authority-only match set is what the unknown-authority guard
        # inspects. Severity must not fold into that set: a known
        # authority with no alerts at the requested severity is not
        # an unknown authority (issue #26).
        authority_matching = (
            [a for a in response.alerts if a.provenance.authority == authority]
            if authority
            else []
        )
        matching = authority_matching if authority else response.alerts
        if severity:
            matching = [a for a in matching if a.severity == severity]

        available_authorities = None
        if authority and not authority_matching:
            available_authorities = sorted(
                {a.provenance.authority for a in response.alerts}
            )

        total_matched = len(matching)
        returned_alerts = matching[:limit] if limit is not None else matching
        truncated = len(returned_alerts) < total_matched

        return ListAlertsResult(
            alerts=returned_alerts,
            sources=response.sources,
            partial=response.partial,
            truncated=truncated,
            total_matched=total_matched,
            returned=len(returned_alerts),
            available_authorities=available_authorities,
        )

    @server.tool(
        description=(
            "What this system covers and what it misses: per-source "
            "status, each source's structural gaps (fields that source "
            "can never supply), and hazard-family coverage. Call this "
            "before trusting an absence of alerts for a given hazard "
            "or region -- 'no alerts' from `list_alerts` can mean a "
            "source is down, or that no source covers that hazard at "
            "all (see `uncovered_hazards`)."
        )
    )
    def list_sources() -> ListSourcesResult:
        response = _collect_cached(default_adapters())
        report = build_sources_response(default_adapters(), response)
        return ListSourcesResult(
            sources=[s.model_dump() for s in report.sources],
            hazard_coverage=report.hazard_coverage,
            uncovered_hazards=report.uncovered_hazards,
        )

    @server.tool(
        description=(
            "Hazard family (earthquake, tsunami, volcano, wildfire, "
            "drought, flood, storm/wind, heat, rain, snow/ice) mapped "
            "to the source ids that answered it this fetch, plus "
            "`uncovered_hazards`: families with zero contributing "
            "sources. This is how to learn the system has, for "
            "example, no tsunami source -- rather than concluding "
            "there are no tsunamis. Classification is heuristic "
            "(keyword matching on each alert's free-text `event` "
            "field), not authoritative, and never alters alert data "
            "elsewhere."
        )
    )
    def get_hazard_coverage() -> HazardCoverageResult:
        response = _collect_cached(default_adapters())
        report = build_sources_response(default_adapters(), response)
        # Every declared family appears even with zero sources, so a
        # caller can enumerate what "hazard family" means here without
        # a live fetch that happens to be empty for a covered family.
        coverage = {family: [] for family in HAZARD_KEYWORDS}
        coverage.update(report.hazard_coverage)
        return HazardCoverageResult(
            hazard_coverage=coverage,
            uncovered_hazards=report.uncovered_hazards,
        )

    @server.tool(
        description=(
            "Cross-source duplicate groups -- alerts from different "
            "sources believed to describe the same hazard record "
            "(exact match on event + area, never fuzzy). Reports only: "
            "never merges or removes anything `list_alerts` returns. "
            "`ambiguous_duplicate_groups` counts candidate groups that "
            "were found but discarded because one source's own ids "
            "disagreed with the grouping -- a hint that duplicates may "
            "exist without a confident pairing, not a number to ignore."
        )
    )
    def find_duplicates() -> FindDuplicatesResult:
        response = _collect_cached(default_adapters())
        return FindDuplicatesResult(
            duplicate_groups=[g.model_dump() for g in response.duplicate_groups],
            ambiguous_duplicate_groups=response.ambiguous_duplicate_groups,
        )

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
