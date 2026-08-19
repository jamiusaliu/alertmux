"""Persistent across-poll seen-state, so the same alert is not
re-notified on every run.

A JSON file keyed by alert id. Alert ids are stable by design (D2: derived
from `capurl`, not a request-scoped feature id), so "have we notified
about this id before" is a question the id can actually answer -- an
unstable id would make this whole module meaningless, since every poll
would look like a fresh batch.

Deliberately a flat JSON file, not a database: the operator running a
single self-hosted notifier process does not need concurrent-writer
semantics, and a JSON file is trivially inspectable and diffable when
something needs auditing ("did we actually notify about this alert?").
Writes are atomic (write to a temp file, then `os.replace`) so a crash
mid-write cannot corrupt the file into an unreadable state and force a
resend-everything on the next run -- the notified-once guarantee would be
worthless if the mechanism that remembers it were the fragile part.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


class StateStore:
    """Tracks which alert ids have already been notified, and when.

    Usage:
        store = StateStore(path)
        store.load()
        if not store.seen(alert.id):
            ... send ...
            store.record(alert.id, rule="nigeria-severe")
        store.save()
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        # alert_id -> {"notified_at": iso8601 str, "rule": str}
        self._entries: dict[str, dict] = {}

    def load(self) -> None:
        """Load state from disk. A missing file means "never run before"
        and is not an error -- the store simply starts empty."""
        if not self.path.exists():
            self._entries = {}
            return
        with self.path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        # Tolerate an empty or legacy-shaped file rather than raising --
        # losing the seen-set is a resend risk, not a crash risk.
        self._entries = raw.get("entries", {}) if isinstance(raw, dict) else {}

    def save(self) -> None:
        """Atomic write: build the file fully in a temp file in the same
        directory, then `os.replace` it over the target. A process killed
        mid-write leaves the old file intact rather than a half-written
        JSON file that fails to parse on the next run."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": self._entries}
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".alertmux-notify-state-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def seen(self, alert_id: str) -> bool:
        return alert_id in self._entries

    def record(self, alert_id: str, rule: str, *, when: datetime | None = None) -> None:
        when = when or datetime.now(tz=timezone.utc)
        self._entries[alert_id] = {"notified_at": when.isoformat(), "rule": rule}

    def prune(self, max_age_days: int, *, now: datetime | None = None) -> int:
        """Drop entries older than `max_age_days`. Returns the count
        removed. Necessary so the file does not grow forever -- alert ids
        are one-shot (an alert eventually expires or ages out of every
        upstream feed), so there is no reason to remember one past the
        window it could plausibly still be re-served by a source."""
        now = now or datetime.now(tz=timezone.utc)
        cutoff = now - timedelta(days=max_age_days)
        to_remove = []
        for alert_id, entry in self._entries.items():
            try:
                notified_at = datetime.fromisoformat(entry["notified_at"])
            except (KeyError, ValueError):
                # Malformed entry: prune it too rather than let a single
                # bad row live forever.
                to_remove.append(alert_id)
                continue
            if notified_at < cutoff:
                to_remove.append(alert_id)
        for alert_id in to_remove:
            del self._entries[alert_id]
        return len(to_remove)

    def __len__(self) -> int:
        return len(self._entries)
