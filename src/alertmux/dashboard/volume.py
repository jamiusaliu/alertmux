"""Append-only volume history: one snapshot per refresh.

Nothing before v0.6 recorded how alert volume moved over time, so a
source going quiet (item 4 of the dashboard spec) was invisible until a
human happened to notice. This module is the fix: every time the
dashboard's own collector performs a *fresh* fetch (a cache hit records
nothing -- see `dashboard/collector.py`), it appends one line here.

Deliberately JSONL, not a database, for the same reason `notify/state.py`
is a flat JSON file: this is a single local operator's tool, one writer,
and a format that is trivially inspectable (`tail -f`) when something
needs auditing. Append-only keeps a crash mid-write from corrupting
history the way an in-place rewrite could; `prune()` is a separate,
explicit step so the file cannot grow forever.

**History begins when recording began.** A freshly started dashboard has
an empty file. That is not "zero alerts ever" -- it is "no observations
yet" -- and `read()` reports `recording_started_at=None` for an empty
file specifically so the caller (the dashboard page) can render "no data
yet" instead of a chart that implies a flat zero.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from alertmux.query import AlertsResponse

DEFAULT_VOLUME_PATH = "alertmux_dashboard_volume.jsonl"

# Keep the file small and prunable, per the brief. 2000 snapshots is
# comfortably multiple weeks of history even at a refresh every few
# minutes, and pruning is O(n) but only runs on writes, not reads.
DEFAULT_MAX_ENTRIES = 2000


@dataclass
class SourceVolume:
    ok: bool
    alert_count: int
    latency_ms: int | None = None


@dataclass
class VolumeSnapshot:
    timestamp: datetime
    partial: bool
    total_alerts: int
    sources: dict[str, SourceVolume] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "partial": self.partial,
            "total_alerts": self.total_alerts,
            "sources": {
                source_id: {
                    "ok": sv.ok,
                    "alert_count": sv.alert_count,
                    "latency_ms": sv.latency_ms,
                }
                for source_id, sv in self.sources.items()
            },
        }

    @classmethod
    def from_response(
        cls, response: AlertsResponse, *, when: datetime | None = None
    ) -> "VolumeSnapshot":
        when = when or datetime.now(tz=timezone.utc)
        sources = {
            status.source_id: SourceVolume(
                ok=status.ok,
                alert_count=status.alert_count,
                latency_ms=status.latency_ms,
            )
            for status in response.sources
        }
        return cls(
            timestamp=when,
            partial=response.partial,
            total_alerts=len(response.alerts),
            sources=sources,
        )


class VolumeRecorder:
    """Owns one JSONL file of `VolumeSnapshot`s.

    Usage:
        recorder = VolumeRecorder(path)
        recorder.record(response)          # one line, then auto-prune
        entries, started_at = recorder.read()
    """

    def __init__(self, path: str | Path, *, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.path = Path(path)
        self.max_entries = max_entries

    def record(self, response: AlertsResponse, *, when: datetime | None = None) -> None:
        """Append one snapshot built from an already-collected
        `AlertsResponse`. Never fetches anything itself -- the caller
        (the dashboard's own TTL-cached collector) decides when a
        "refresh" happened."""
        snapshot = VolumeSnapshot.from_response(response, when=when)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(snapshot.to_json(), sort_keys=True))
            fh.write("\n")
        self.prune(self.max_entries)

    def read(self, *, limit: int | None = None) -> list[dict]:
        """Return recorded snapshots, oldest first, tolerating malformed
        lines (skipped, never fatal -- one bad line must not hide the
        rest of the history). A missing file reads as an empty list,
        exactly like a freshly started recorder -- both mean "no
        observations yet"."""
        if not self.path.exists():
            return []
        entries: list[dict] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if limit is not None:
            entries = entries[-limit:]
        return entries

    def recording_started_at(self) -> str | None:
        """Timestamp of the earliest recorded snapshot, or None if
        nothing has been recorded yet. Used by the dashboard to
        distinguish "no data yet" from a real all-zero history."""
        entries = self.read(limit=1)
        if not entries:
            return None
        return entries[0].get("timestamp")

    def prune(self, max_entries: int) -> int:
        """Keep only the most recent `max_entries` lines. Returns the
        count removed. Rewrites the file via a temp file + os.replace,
        same atomic-write discipline as `notify/state.py`, so a crash
        mid-prune cannot corrupt the file."""
        entries = self.read()
        if len(entries) <= max_entries:
            return 0
        kept = entries[-max_entries:]
        removed = len(entries) - len(kept)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".alertmux-dashboard-volume-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for entry in kept:
                    fh.write(json.dumps(entry, sort_keys=True))
                    fh.write("\n")
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return removed
