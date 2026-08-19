"""Load notifier config from a TOML file plus environment variables.

Credentials (SMTP username/password) come from the config file or from
environment variables, and are never logged, never echoed, and never
included in an exception message. `SmtpConfig.password` is a pydantic
`SecretStr` specifically so an accidental `str(config)`, `repr(config)`,
`logging.info(config)` or exception interpolation prints `**********`
instead of the secret -- see `tests/test_notify_config.py`'s explicit
assertions on this.

Environment variables, when set, override the config file for the SMTP
block only (rules always come from the file). This lets an operator keep
`notify.toml` in version control with everything except the password, and
supply the password via `ALERTMUX_SMTP_PASSWORD` at deploy time.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from alertmux.notify.rules import DEFAULT_MAX_PER_RUN, SEVERITY_ORDER, RuleConfig

_ENV_PREFIX = "ALERTMUX_SMTP_"


class SmtpConfig(BaseModel):
    """BYO SMTP. No default host, no default sender -- this project ships
    no mail infrastructure (DECISIONS.md D11)."""

    host: str
    port: int = 587
    username: str | None = None
    password: SecretStr | None = None
    use_tls: bool = True
    use_ssl: bool = False
    sender: str
    timeout: float = 30.0

    @model_validator(mode="after")
    def _tls_and_ssl_are_exclusive(self) -> "SmtpConfig":
        if self.use_tls and self.use_ssl:
            raise ValueError(
                "smtp.use_tls and smtp.use_ssl are mutually exclusive "
                "(STARTTLS on a plain connection vs. implicit TLS)"
            )
        return self

    def safe_summary(self) -> str:
        """Everything about this config that is safe to log: never the
        password, and the username only as present/absent."""
        mode = "ssl" if self.use_ssl else ("starttls" if self.use_tls else "plain")
        auth = "authenticated" if self.username else "unauthenticated"
        return f"{self.host}:{self.port} ({mode}, {auth}, sender={self.sender})"


class StateConfig(BaseModel):
    path: str = "alertmux_notify_state.json"
    # Entries older than this are eligible for pruning. Notified-state
    # rows only need to outlive the window a source might replay an
    # already-seen alert id, so a generous default is cheap.
    prune_after_days: int = 30


class RunLogConfig(BaseModel):
    """Where each real run's outcome (sent/suppressed/failed) is
    appended, so `alertmux-dashboard` can show a failed run without the
    notifier process still being alive. See notify/runlog.py."""

    path: str = "alertmux_notify_runs.jsonl"
    max_entries: int = 500


class RuleConfigModel(BaseModel):
    """The pydantic/TOML-facing shape of a rule. Converts to the frozen
    `rules.RuleConfig` dataclass that actually does the matching, so the
    matching logic stays free of pydantic/TOML concerns entirely."""

    name: str
    to: list[str]
    authority: str | None = None
    source: str | None = None
    event_contains: str | None = None
    area_contains: str | None = None
    severity_at_least: str | None = None
    include_unmapped_severity: bool = True
    # Safe-direction default: a rule left unconfigured is capped, not
    # unlimited (see `rules.DEFAULT_MAX_PER_RUN` and DECISIONS.md D22).
    # TOML has no `null`, so an operator who deliberately wants no ceiling
    # writes `max_per_run = 0` -- `_zero_means_unlimited` below maps that
    # to Python `None`, which is what `rules.RuleConfig`/`runner.py`
    # already treat as "unlimited". A cap of *zero alerts ever* is not a
    # configuration anyone wants, so reusing 0 as the "unlimited" sentinel
    # costs nothing.
    max_per_run: int | None = DEFAULT_MAX_PER_RUN
    digest: bool = False

    @field_validator("to")
    @classmethod
    def _at_least_one_recipient(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("rule must have at least one 'to' recipient")
        return v

    @field_validator("severity_at_least")
    @classmethod
    def _known_severity(cls, v: str | None) -> str | None:
        if v is not None and v not in SEVERITY_ORDER:
            raise ValueError(
                f"severity_at_least={v!r} is not a CAP severity "
                f"({sorted(SEVERITY_ORDER)}); check spelling and capitalisation"
            )
        return v

    @field_validator("max_per_run")
    @classmethod
    def _zero_means_unlimited(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v == 0:
            return None
        if v < 0:
            raise ValueError(
                f"max_per_run={v!r} must be a positive cap, or 0 for "
                "deliberately unlimited"
            )
        return v

    def to_rule_config(self) -> RuleConfig:
        return RuleConfig(
            name=self.name,
            to=tuple(self.to),
            authority=self.authority,
            source=self.source,
            event_contains=self.event_contains,
            area_contains=self.area_contains,
            severity_at_least=self.severity_at_least,
            include_unmapped_severity=self.include_unmapped_severity,
            max_per_run=self.max_per_run,
            digest=self.digest,
        )


class NotifierConfig(BaseModel):
    smtp: SmtpConfig
    rules: list[RuleConfigModel] = Field(default_factory=list)
    state: StateConfig = Field(default_factory=StateConfig)
    run_log: RunLogConfig = Field(default_factory=RunLogConfig)

    def rule_configs(self) -> list[RuleConfig]:
        """The pure, immutable `RuleConfig` dataclasses `rules.py` matches
        against, built from the validated pydantic models."""
        return [rule.to_rule_config() for rule in self.rules]


class ConfigError(Exception):
    """Raised for a malformed or invalid notifier config. The message is
    built only from field names and validation reasons -- never from
    credential values, which pydantic's SecretStr already keeps out of
    its own error rendering for the password field, but this wrapper
    exists so nothing upstream ever reaches for `str(raw_dict)` either."""


def _apply_env_overrides(smtp_dict: dict, env: Mapping[str, str]) -> dict:
    overrides = {
        "host": env.get(f"{_ENV_PREFIX}HOST"),
        "port": env.get(f"{_ENV_PREFIX}PORT"),
        "username": env.get(f"{_ENV_PREFIX}USERNAME"),
        "password": env.get(f"{_ENV_PREFIX}PASSWORD"),
        "sender": env.get(f"{_ENV_PREFIX}SENDER"),
        "use_tls": env.get(f"{_ENV_PREFIX}USE_TLS"),
        "use_ssl": env.get(f"{_ENV_PREFIX}USE_SSL"),
    }
    merged = dict(smtp_dict)
    for key, value in overrides.items():
        if value is None:
            continue
        if key == "port":
            merged[key] = int(value)
        elif key in ("use_tls", "use_ssl"):
            merged[key] = value.strip().lower() in ("1", "true", "yes", "on")
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> NotifierConfig:
    """Load and validate a notifier config file (TOML).

    `env` defaults to `os.environ` and overrides only the `[smtp]` block
    -- see the module docstring. Raises `ConfigError` (never a bare
    pydantic ValidationError string containing the raw file dict, which
    could echo a password left in the file) on anything invalid.
    """
    env = os.environ if env is None else env
    path = Path(path)
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    smtp_raw = raw.get("smtp", {})
    smtp_raw = _apply_env_overrides(smtp_raw, env)
    rules_raw = raw.get("rules", [])
    state_raw = raw.get("state", {})

    try:
        return NotifierConfig(smtp=smtp_raw, rules=rules_raw, state=state_raw)
    except Exception as exc:  # pydantic ValidationError, deliberately broad
        # Field names and reasons only. pydantic's own ValidationError
        # repr does not include SecretStr values, but we still avoid ever
        # interpolating smtp_raw itself into the message.
        raise ConfigError(f"invalid notifier config in {path}: {exc}") from exc
