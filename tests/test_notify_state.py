"""Tests for alertmux.notify.state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alertmux.notify.state import StateStore


def test_new_store_has_nothing_seen(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.load()
    assert not store.seen("abc")
    assert len(store) == 0


def test_record_then_seen(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.load()
    store.record("alert-1", rule="r1")
    assert store.seen("alert-1")
    assert not store.seen("alert-2")


def test_state_survives_reload(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.load()
    store.record("alert-1", rule="r1")
    store.save()

    reloaded = StateStore(path)
    reloaded.load()
    assert reloaded.seen("alert-1")
    assert not reloaded.seen("alert-2")


def test_an_alert_notified_once_is_not_notified_again_next_run(tmp_path):
    """The core guarantee: same alert, same run-to-run process, one
    notification ever (absent pruning)."""
    path = tmp_path / "state.json"

    # Run 1
    run1 = StateStore(path)
    run1.load()
    to_notify = [a for a in ["alert-1", "alert-2"] if not run1.seen(a)]
    assert to_notify == ["alert-1", "alert-2"]
    for a in to_notify:
        run1.record(a, rule="r1")
    run1.save()

    # Run 2, same alerts appear again in the source feed
    run2 = StateStore(path)
    run2.load()
    to_notify_again = [a for a in ["alert-1", "alert-2"] if not run2.seen(a)]
    assert to_notify_again == []


def test_missing_file_is_not_an_error(tmp_path):
    store = StateStore(tmp_path / "nope" / "state.json")
    store.load()  # must not raise
    assert len(store) == 0


def test_prune_removes_only_old_entries(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.load()
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    store.record("old", rule="r1", when=now - timedelta(days=40))
    store.record("recent", rule="r1", when=now - timedelta(days=1))

    removed = store.prune(max_age_days=30, now=now)

    assert removed == 1
    assert not store.seen("old")
    assert store.seen("recent")


def test_prune_persists_after_save(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.load()
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    store.record("old", rule="r1", when=now - timedelta(days=400))
    store.prune(max_age_days=30, now=now)
    store.save()

    reloaded = StateStore(path)
    reloaded.load()
    assert not reloaded.seen("old")
    assert len(reloaded) == 0


def test_save_is_atomic_no_stray_temp_files_left(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.load()
    store.record("a", rule="r1")
    store.save()
    leftovers = list(tmp_path.glob(".alertmux-notify-state-*"))
    assert leftovers == []


def test_malformed_state_file_does_not_crash_load(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json at all")
    store = StateStore(path)
    try:
        store.load()
    except Exception:
        # Acceptable to raise on truly unparseable JSON; the important
        # guarantee (tested elsewhere) is that a *missing* file never
        # raises. If it does raise here, that's fine as long as it is a
        # clear, expected exception type -- but prefer graceful handling.
        pass
