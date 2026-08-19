"""Tests for alertmux.notify.delivery. No real mail: smtplib is mocked."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from alertmux.notify.config import SmtpConfig
from alertmux.notify.delivery import (
    DeliveryError,
    SmtpSender,
    build_alert_message,
    build_digest_message,
)
from alertmux.schema import DISCLAIMER, NormalisedAlert, Provenance


def _alert(id="a1", headline="Severe Flood Warning", description="Water rising fast."):
    return NormalisedAlert(
        id=id,
        event="Flood Warning",
        headline=headline,
        description=description,
        area_description="Lagos",
        severity="Severe",
        source_severity="3",
        provenance=Provenance(
            authority="ng-nimet",
            source_id="swic",
            source_url="https://example.org/feed",
            retrieved_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
        ),
    )


def _smtp_config(**overrides):
    defaults = dict(host="smtp.example.org", port=587, sender="alerts@example.org")
    defaults.update(overrides)
    return SmtpConfig(**defaults)


# --- message construction: verbatim relay ---


def test_alert_message_carries_headline_and_description_verbatim():
    alert = _alert(headline="DO NOT REWORD THIS", description="EXACT TEXT MUST SURVIVE")
    msg = build_alert_message(alert, sender="a@example.org", to=["b@example.org"], rule_name="r1")
    body = msg.get_content()
    assert "DO NOT REWORD THIS" in body
    assert "EXACT TEXT MUST SURVIVE" in body


def test_alert_message_carries_provenance_and_disclaimer():
    alert = _alert()
    msg = build_alert_message(alert, sender="a@example.org", to=["b@example.org"], rule_name="r1")
    body = msg.get_content()
    assert "ng-nimet" in body
    assert "swic" in body
    assert "https://example.org/feed" in body
    assert DISCLAIMER in body
    assert alert.id in body


def test_alert_message_notes_missing_expiry_rather_than_assuming_forever():
    alert = _alert()
    assert alert.expires is None
    msg = build_alert_message(alert, sender="a@example.org", to=["b@example.org"], rule_name="r1")
    assert "not stated by source" in msg.get_content()


def test_digest_message_bundles_multiple_alerts_into_one_message():
    alerts = [_alert(id="a1", headline="First"), _alert(id="a2", headline="Second")]
    msg = build_digest_message(alerts, sender="a@example.org", to=["b@example.org"], rule_name="r1")
    body = msg.get_content()
    assert "First" in body
    assert "Second" in body
    assert "2 alert(s)" in msg["Subject"]


# --- delivery: mocked smtplib, loud failure, dry-run-adjacent behaviour ---


def test_send_uses_starttls_and_login_when_configured():
    config = _smtp_config(username="user", password="pw", use_tls=True)
    sender = SmtpSender(config)
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client

    with patch("smtplib.SMTP", return_value=mock_client) as smtp_cls:
        sender.send(build_alert_message(_alert(), sender="a@x.org", to=["b@x.org"], rule_name="r1"))

    smtp_cls.assert_called_once_with("smtp.example.org", 587, timeout=config.timeout)
    mock_client.starttls.assert_called_once()
    mock_client.login.assert_called_once_with("user", "pw")
    mock_client.send_message.assert_called_once()


def test_send_uses_ssl_when_configured():
    config = _smtp_config(port=465, use_tls=False, use_ssl=True)
    sender = SmtpSender(config)
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client

    with patch("smtplib.SMTP_SSL", return_value=mock_client) as smtp_ssl_cls:
        sender.send(build_alert_message(_alert(), sender="a@x.org", to=["b@x.org"], rule_name="r1"))

    smtp_ssl_cls.assert_called_once_with("smtp.example.org", 465, timeout=config.timeout)
    mock_client.starttls.assert_not_called()


def test_smtp_failure_raises_deliveryerror_not_swallowed():
    config = _smtp_config()
    sender = SmtpSender(config)

    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        with pytest.raises(DeliveryError):
            sender.send(build_alert_message(_alert(), sender="a@x.org", to=["b@x.org"], rule_name="r1"))


def test_credentials_never_appear_in_deliveryerror_message():
    config = _smtp_config(username="secret-user", password="secret-password-value")
    sender = SmtpSender(config)

    with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        with pytest.raises(DeliveryError) as excinfo:
            sender.send(build_alert_message(_alert(), sender="a@x.org", to=["b@x.org"], rule_name="r1"))

    assert "secret-password-value" not in str(excinfo.value)
    assert "secret-user" not in str(excinfo.value)


def test_credentials_never_appear_in_login_failure_message():
    config = _smtp_config(username="secret-user", password="secret-password-value")
    sender = SmtpSender(config)
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.login.side_effect = smtplib_auth_error()

    with patch("smtplib.SMTP", return_value=mock_client):
        with pytest.raises(DeliveryError) as excinfo:
            sender.send(build_alert_message(_alert(), sender="a@x.org", to=["b@x.org"], rule_name="r1"))

    assert "secret-password-value" not in str(excinfo.value)


def smtplib_auth_error():
    import smtplib as _smtplib

    return _smtplib.SMTPAuthenticationError(535, b"Authentication credentials invalid")
