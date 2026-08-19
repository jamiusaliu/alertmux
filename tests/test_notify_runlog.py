"""Tests for notify/runlog.py -- the append-only notifier run log."""

from __future__ import annotations

from datetime import datetime, timezone

from alertmux.notify.runlog import RunLogStore
from alertmux.notify.runner import RunReport

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _clean_report() -> RunReport:
    return RunReport(matched_by_rule={"all": 2}, sent_count=2)


def _failed_report() -> RunReport:
    return RunReport(
        matched_by_rule={"all": 1},
        sent_count=0,
        failures=["rule 'all' alert a1: SMTP timeout"],
    )


def test_append_then_read_round_trips(tmp_path):
    store = RunLogStore(tmp_path / "runs.jsonl")
    store.append(_clean_report(), when=NOW)

    entries = store.read()
    assert len(entries) == 1
    assert entries[0]["sent_count"] == 2
    assert entries[0]["ok"] is True
    assert entries[0]["timestamp"] == NOW.isoformat()


def test_failed_run_is_recorded_with_ok_false(tmp_path):
    store = RunLogStore(tmp_path / "runs.jsonl")
    store.append(_failed_report(), when=NOW)

    entries = store.read()
    assert entries[0]["ok"] is False
    assert "SMTP timeout" in entries[0]["failures"][0]


def test_missing_file_reads_as_empty_list(tmp_path):
    store = RunLogStore(tmp_path / "nope.jsonl")
    assert store.read() == []


def test_malformed_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "runs.jsonl"
    store = RunLogStore(path)
    store.append(_clean_report(), when=NOW)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
    store.append(_failed_report(), when=NOW)

    entries = store.read()
    assert len(entries) == 2


def test_prune_keeps_only_the_most_recent_entries(tmp_path):
    store = RunLogStore(tmp_path / "runs.jsonl", max_entries=3)
    for i in range(5):
        store.append(_clean_report(), when=NOW)

    entries = store.read()
    assert len(entries) == 3


def test_read_survives_a_restart(tmp_path):
    path = tmp_path / "runs.jsonl"
    store = RunLogStore(path)
    store.append(_clean_report(), when=NOW)

    reopened = RunLogStore(path)
    entries = reopened.read()
    assert len(entries) == 1
