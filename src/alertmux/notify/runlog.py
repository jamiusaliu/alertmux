"""Append-only run log: what a notifier run actually did.

`runner.RunReport` carries `sent_count`, `suppressed_by_rate_limit` and
`failures`, but until now nothing persisted it (see runner.py's module
docstring). That is fine for a process an operator is watching live, and
wrong for the case that matters most: an unattended cron job whose SMTP
server starts rejecting mail. The process exits non-zero, cron mails
nobody (or the operator ignores the one cron email that eventually gets
lost the same way the alerts are), and the next visible sign of trouble
is a hazard nobody was warned about.

This module gives every run -- clean or failed -- a durable, append-only
trace on disk, so `alertmux-dashboard` can show it without depending on
the notifier process still being alive. Same JSONL-plus-atomic-prune
shape as `dashboard/volume.py`, deliberately: single local writer, one
line per event, trivially inspectable.

**Only real runs are logged, never dry runs.** A dry run previews what
*would* happen; logging it as an outcome would misrepresent nothing
having been sent as something having run. This mirrors `StateStore`,
which likewise is never written to on a dry run.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from alertmux.notify.runner import RunReport

DEFAULT_RUN_LOG_PATH = "alertmux_notify_runs.jsonl"

# A run log entry is tiny (a handful of counts and short strings), so a
# generous cap costs little and still bounds the file.
DEFAULT_MAX_ENTRIES = 500


class RunLogStore:
    """Owns one JSONL file of run outcomes.

    Usage:
        log = RunLogStore(path)
        log.append(report)                 # one line, then auto-prune
        entries = log.read()
    """

    def __init__(self, path: str | Path, *, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.path = Path(path)
        self.max_entries = max_entries

    def append(self, report: RunReport, *, when: datetime | None = None) -> None:
        when = when or datetime.now(tz=timezone.utc)
        entry = {
            "timestamp": when.isoformat(),
            "ok": report.ok,
            "sent_count": report.sent_count,
            "matched_by_rule": report.matched_by_rule,
            "unevaluable_by_rule": report.unevaluable_by_rule,
            "suppressed_by_rate_limit": report.suppressed_by_rate_limit,
            "failures": report.failures,
            "warnings": report.warnings,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True))
            fh.write("\n")
        self.prune(self.max_entries)

    def read(self, *, limit: int | None = None) -> list[dict]:
        """Return logged runs, oldest first, skipping malformed lines
        rather than raising -- one corrupted entry must not hide the
        rest of the run history. A missing file reads as an empty list
        (never run, or never failed/succeeded since the log started)."""
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

    def prune(self, max_entries: int) -> int:
        """Keep only the most recent `max_entries` lines. Same atomic
        temp-file + os.replace discipline as `notify/state.py` and
        `dashboard/volume.py`."""
        entries = self.read()
        if len(entries) <= max_entries:
            return 0
        kept = entries[-max_entries:]
        removed = len(entries) - len(kept)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".alertmux-notify-runs-", suffix=".tmp"
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
