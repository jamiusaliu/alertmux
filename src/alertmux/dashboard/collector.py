"""The dashboard's own cached collect path, and its source-health memory.

Deliberately a second, small cache, not a dependency on `api.py`'s
(`api._collect_shared` is private, and two open PRs are actively
rewriting that module's cache -- see docs/DECISIONS.md). This mirrors
its TTL policy (a full fetch is good for 60s, a partial one for 10s, so
a recovered source becomes visible quickly) but owns its state
independently, so the dashboard has no import-time coupling to a module
under active external rework.

**"Reuse the cached collect path" means reuse `alertmux.query.collect`
behind a TTL cache -- never fetch WMO on every page load.** This module
is that cache. It also owns the one piece of state neither `query.py`
nor `api.py` track: per-source health *across* polls -- last successful
fetch, and a consecutive-failure count -- which item 1 of the dashboard
spec needs and no existing module computes, because every existing
consumer only ever looks at one fetch at a time.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from alertmux.dashboard.volume import VolumeRecorder
from alertmux.query import AlertsResponse, collect

CACHE_TTL_SECONDS = 60.0
PARTIAL_CACHE_TTL_SECONDS = 10.0


@dataclass
class SourceHealth:
    source_id: str
    last_ok_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0

    def to_json(self) -> dict:
        return {
            "source_id": self.source_id,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }


class DashboardCollector:
    """Owns the dashboard's TTL cache, its cross-poll source-health
    memory, and (optionally) a `VolumeRecorder` that gets one snapshot
    per *fresh* fetch (never per cache hit, never per HTTP request)."""

    def __init__(self, volume_recorder: VolumeRecorder | None = None):
        self._lock = threading.Lock()
        self._cache: tuple[float, AlertsResponse] | None = None
        self._health: dict[str, SourceHealth] = {}
        self._volume_recorder = volume_recorder

    def _ttl_for(self, response: AlertsResponse) -> float:
        return PARTIAL_CACHE_TTL_SECONDS if response.partial else CACHE_TTL_SECONDS

    def _update_health(self, response: AlertsResponse, *, when: datetime) -> None:
        for status in response.sources:
            health = self._health.setdefault(
                status.source_id, SourceHealth(source_id=status.source_id)
            )
            if status.ok:
                health.last_ok_at = when
                health.consecutive_failures = 0
                health.last_error = None
            else:
                health.consecutive_failures += 1
                health.last_error = status.error

    def collect(self, adapters) -> AlertsResponse:
        """Return a fresh-enough `AlertsResponse`, fetching only when the
        cache has expired. On every *fresh* fetch (not a cache hit):
        updates cross-poll source health, and records one volume
        snapshot if a recorder was configured."""
        with self._lock:
            cached = self._cache
            if cached is not None and (time.monotonic() - cached[0]) < self._ttl_for(cached[1]):
                return cached[1]

        response = collect(adapters)
        now = datetime.now(tz=timezone.utc)

        with self._lock:
            self._cache = (time.monotonic(), response)
            self._update_health(response, when=now)

        if self._volume_recorder is not None:
            self._volume_recorder.record(response, when=now)

        return response

    @property
    def volume_recorder(self) -> VolumeRecorder | None:
        return self._volume_recorder

    def health_snapshot(self) -> dict[str, SourceHealth]:
        with self._lock:
            return dict(self._health)

    def clear(self) -> None:
        """Used by tests; harmless in production."""
        with self._lock:
            self._cache = None
            self._health = {}
