"""Tests for the alertmux-notify CLI. Mocks alertmux.query.collect and
SmtpSender so no network is touched."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from alertmux.notify import cli
from alertmux.query import AlertsResponse
from alertmux.schema import NormalisedAlert, Provenance

NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)

CONFIG_TOML = """
[smtp]
host = "smtp.example.org"
sender = "alerts@example.org"

[[rules]]
name = "all"
to = ["ops@example.org"]
"""

SEVERITY_CONFIG_TOML = """
[smtp]
host = "smtp.example.org"
sender = "alerts@example.org"

[[rules]]
name = "severe-only"
to = ["ops@example.org"]
severity_at_least = "Severe"
"""


def _alert(id="a1", source_id="swic", severity="Severe"):
    return NormalisedAlert(
        id=id,
        event="Flood Warning",
        headline="h",
        description="d",
        severity=severity,
        provenance=Provenance(
            authority="ng-nimet",
            source_id=source_id,
            source_url="https://example.org",
            retrieved_at=NOW,
        ),
    )


def _response(alerts):
    return AlertsResponse(alerts=alerts, retrieved_at=NOW)


def _write(tmp_path, text):
    p = tmp_path / "notify.toml"
    p.write_text(text)
    return str(p)


def test_dry_run_sends_nothing_and_exits_zero(tmp_path, capsys):
    config_path = _write(tmp_path, CONFIG_TOML)
    with patch("alertmux.notify.cli.collect", return_value=_response([_alert()])), patch(
        "alertmux.notify.cli.SmtpSender"
    ) as sender_cls:
        rc = cli.main(["--config", config_path, "--dry-run"])
    sender_cls.assert_not_called()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dry run" in out


def test_missing_config_file_exits_nonzero(tmp_path, capsys):
    rc = cli.main(["--config", str(tmp_path / "nope.toml")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "alertmux-notify:" in err


def test_real_run_sends_via_smtpsender_and_exits_zero(tmp_path, capsys):
    config_path = _write(tmp_path, CONFIG_TOML)
    state_dir = tmp_path / "state.json"

    with patch("alertmux.notify.cli.collect", return_value=_response([_alert()])), patch(
        "alertmux.notify.cli.SmtpSender"
    ) as sender_cls, patch("alertmux.notify.cli.StateStore") as state_cls:
        mock_state = MagicMock()
        mock_state.seen.return_value = False
        mock_state.prune.return_value = 0
        state_cls.return_value = mock_state
        mock_sender = MagicMock()
        sender_cls.return_value = mock_sender

        rc = cli.main(["--config", config_path])

    assert rc == 0
    mock_sender.send.assert_called_once()
    out = capsys.readouterr().out
    assert "Sent 1 message" in out


def test_smtp_failure_exits_nonzero(tmp_path, capsys):
    from alertmux.notify.delivery import DeliveryError

    config_path = _write(tmp_path, CONFIG_TOML)

    with patch("alertmux.notify.cli.collect", return_value=_response([_alert()])), patch(
        "alertmux.notify.cli.SmtpSender"
    ) as sender_cls, patch("alertmux.notify.cli.StateStore") as state_cls:
        mock_state = MagicMock()
        mock_state.seen.return_value = False
        mock_state.prune.return_value = 0
        state_cls.return_value = mock_state
        mock_sender = MagicMock()
        mock_sender.send.side_effect = DeliveryError("boom")
        sender_cls.return_value = mock_sender

        rc = cli.main(["--config", config_path])

    assert rc == 1
    err = capsys.readouterr().err
    assert "delivery failure" in err.lower()


def test_unmapped_severity_warning_printed_in_dry_run(tmp_path, capsys):
    config_path = _write(tmp_path, SEVERITY_CONFIG_TOML)
    gdacs_alert = _alert(id="g1", source_id="gdacs", severity=None)

    with patch("alertmux.notify.cli.collect", return_value=_response([gdacs_alert])):
        rc = cli.main(["--config", config_path, "--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "gdacs" in out


def test_config_with_no_rules_exits_nonzero(tmp_path, capsys):
    config_path = _write(
        tmp_path,
        """
[smtp]
host = "smtp.example.org"
sender = "a@example.org"
""",
    )
    rc = cli.main(["--config", config_path])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no [[rules]]" in err
