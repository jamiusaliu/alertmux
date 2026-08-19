"""SMTP delivery. stdlib `smtplib` only, no new dependency.

Verbatim relay (principle 1): the message body carries the alert's
official `headline` and `description` unmodified, plus authority, source
URL, retrieval time, onset, expiry and the standing `DISCLAIMER` from
`alertmux.schema`. Nothing here reformats, summarises or "improves"
hazard text -- it only arranges the already-verbatim fields into an email.

Failure is loud (spec): `send_alert`/`send_digest` never swallow an SMTP
exception. They raise `DeliveryError`, whose message is built only from
the SMTP host/port and the exception type/message `smtplib` itself
raised -- never from `SmtpConfig.password`, which is a `SecretStr` and
was never passed to string formatting in the first place.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from alertmux.notify.config import SmtpConfig
from alertmux.schema import DISCLAIMER, NormalisedAlert


class DeliveryError(Exception):
    """SMTP delivery failed. Message never contains credentials."""


def _format_alert_block(alert: NormalisedAlert) -> str:
    """One alert's verbatim block. Every hazard-content field is relayed
    exactly as the source supplied it -- no truncation, no rewording."""
    lines = [
        f"Event: {alert.event}",
        f"Authority: {alert.provenance.authority}",
        f"Source: {alert.provenance.source_id} ({alert.provenance.source_url})",
        f"Retrieved: {alert.provenance.retrieved_at.isoformat()}",
    ]
    if alert.area_description:
        lines.append(f"Area: {alert.area_description}")
    if alert.severity:
        lines.append(f"Severity: {alert.severity} (source: {alert.source_severity})")
    elif alert.source_severity:
        lines.append(
            f"Severity: not mapped by alertmux (source reported: "
            f"{alert.source_severity!r}) -- see unmapped_fields"
        )
    if alert.onset:
        lines.append(f"Onset: {alert.onset.isoformat()}")
    if alert.expires:
        lines.append(f"Expires: {alert.expires.isoformat()}")
    else:
        lines.append("Expires: not stated by source (unknown, treat as potentially live)")
    lines.append("")
    if alert.headline:
        lines.append(alert.headline)
        lines.append("")
    if alert.description:
        lines.append(alert.description)
        lines.append("")
    if alert.instruction:
        lines.append("Instruction:")
        lines.append(alert.instruction)
        lines.append("")
    lines.append(f"Alert id: {alert.id}")
    return "\n".join(lines)


def build_alert_message(
    alert: NormalisedAlert, *, sender: str, to: list[str], rule_name: str
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[alertmux/{rule_name}] {alert.event} - {alert.provenance.authority}"
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    body = _format_alert_block(alert) + "\n\n---\n" + DISCLAIMER
    msg.set_content(body)
    return msg


def build_digest_message(
    alerts: list[NormalisedAlert], *, sender: str, to: list[str], rule_name: str
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[alertmux/{rule_name}] digest: {len(alerts)} alert(s)"
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    blocks = [_format_alert_block(a) for a in alerts]
    body = ("\n\n" + "-" * 40 + "\n\n").join(blocks) + "\n\n---\n" + DISCLAIMER
    msg.set_content(body)
    return msg


class SmtpSender:
    """Thin wrapper over smtplib for one SMTP relay, configured once and
    reused for a run's sends. Not a connection pool -- one connection per
    call keeps the failure mode simple (see `send`) at the cost of a
    little latency, which is fine for a poll-driven notifier, not a
    high-throughput mailer.
    """

    def __init__(self, config: SmtpConfig):
        self.config = config

    def send(self, message: EmailMessage) -> None:
        """Send one already-built message. Raises `DeliveryError` on any
        SMTP-layer failure -- connection refused, auth failure, recipient
        refused, timeout. Never swallowed."""
        config = self.config
        try:
            if config.use_ssl:
                smtp_cls = smtplib.SMTP_SSL
            else:
                smtp_cls = smtplib.SMTP
            with smtp_cls(config.host, config.port, timeout=config.timeout) as client:
                if config.use_tls and not config.use_ssl:
                    client.starttls()
                if config.username is not None:
                    password = (
                        config.password.get_secret_value()
                        if config.password is not None
                        else ""
                    )
                    client.login(config.username, password)
                client.send_message(message)
        except Exception as exc:  # noqa: BLE001 - re-raised as DeliveryError below
            # Deliberately does not interpolate `config` or `message`
            # wholesale -- only host/port (never a secret) and the
            # exception's own type/message, which smtplib does not put
            # credentials into.
            raise DeliveryError(
                f"SMTP delivery to {config.host}:{config.port} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
