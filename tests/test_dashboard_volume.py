"""Tests for dashboard/volume.py -- the append-only volume-history recorder."""

from __future__ import annotations

from datetime import datetime, timezone

from alertmux.dashboard.volume import VolumeRecorder
from alertmux.query import AlertsResponse, SourceStatus

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _response(*, partial=False, statuses=None, alert_count=0):
    return AlertsResponse(
        alerts=[],
        sources=statuses or [],
        partial=partial,
        retrieved_at=NOW,
    )


def test_record_then_read_round_trips(tmp_path):
    recorder = VolumeRecorder(tmp_path / "volume.jsonl")
    statuses = [SourceStatus(source_id="usgs", ok=True, alert_count=5, latency_ms=120)]
    response = _response(statuses=statuses)
    recorder.record(response, when=NOW)

    entries = recorder.read()
    assert len(entries) == 1
    assert entries[0]["timestamp"] == NOW.isoformat()
    assert entries[0]["sources"]["usgs"]["alert_count"] == 5
    assert entries[0]["partial"] is False


def test_empty_history_has_no_recording_started_at(tmp_path):
    recorder = VolumeRecorder(tmp_path / "volume.jsonl")
    assert recorder.read() == []
    assert recorder.recording_started_at() is None


def test_recording_started_at_is_the_earliest_snapshot(tmp_path):
    recorder = VolumeRecorder(tmp_path / "volume.jsonl")
    recorder.record(_response(), when=NOW)
    assert recorder.recording_started_at() == NOW.isoformat()


def test_prune_keeps_only_the_most_recent_entries(tmp_path):
    recorder = VolumeRecorder(tmp_path / "volume.jsonl", max_entries=3)
    for _ in range(5):
        recorder.record(_response(), when=NOW)

    assert len(recorder.read()) == 3


def test_malformed_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "volume.jsonl"
    recorder = VolumeRecorder(path)
    recorder.record(_response(), when=NOW)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("garbage\n")
    recorder.record(_response(), when=NOW)

    assert len(recorder.read()) == 2


def test_survives_a_restart(tmp_path):
    path = tmp_path / "volume.jsonl"
    recorder = VolumeRecorder(path)
    recorder.record(_response(), when=NOW)

    reopened = VolumeRecorder(path)
    assert len(reopened.read()) == 1
