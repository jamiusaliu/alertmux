"""Tests for alertmux.notify.config, including the credential-safety
guarantees required by the notifier spec."""

from __future__ import annotations

import pytest

from alertmux.notify.config import ConfigError, SmtpConfig, load_config

BASIC_TOML = """
[smtp]
host = "smtp.example.org"
port = 587
username = "alerts@example.org"
password = "super-secret-password"
sender = "alerts@example.org"

[[rules]]
name = "nigeria-severe"
to = ["ops@example.org"]
authority = "ng-nimet"
severity_at_least = "Severe"
"""


def _write(tmp_path, text):
    p = tmp_path / "notify.toml"
    p.write_text(text)
    return p


def test_loads_smtp_and_rules(tmp_path):
    path = _write(tmp_path, BASIC_TOML)
    config = load_config(path, env={})
    assert config.smtp.host == "smtp.example.org"
    assert config.smtp.port == 587
    assert config.smtp.password.get_secret_value() == "super-secret-password"
    rules = config.rule_configs()
    assert len(rules) == 1
    assert rules[0].name == "nigeria-severe"
    assert rules[0].severity_at_least == "Severe"
    assert rules[0].include_unmapped_severity is True  # safe default


def test_env_overrides_password_and_host(tmp_path):
    path = _write(tmp_path, BASIC_TOML)
    env = {
        "ALERTMUX_SMTP_PASSWORD": "env-password",
        "ALERTMUX_SMTP_HOST": "smtp.env.example.org",
    }
    config = load_config(path, env=env)
    assert config.smtp.host == "smtp.env.example.org"
    assert config.smtp.password.get_secret_value() == "env-password"
    # username untouched by env
    assert config.smtp.username == "alerts@example.org"


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.toml", env={})


def test_invalid_toml_raises_config_error(tmp_path):
    path = _write(tmp_path, "this is not [valid toml")
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_unknown_severity_value_rejected(tmp_path):
    bad = BASIC_TOML.replace('severity_at_least = "Severe"', 'severity_at_least = "Catastrophic"')
    path = _write(tmp_path, bad)
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_rule_without_recipient_rejected(tmp_path):
    bad = """
[smtp]
host = "smtp.example.org"
sender = "a@example.org"

[[rules]]
name = "broken"
to = []
"""
    path = _write(tmp_path, bad)
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_use_tls_and_use_ssl_are_mutually_exclusive(tmp_path):
    bad = """
[smtp]
host = "smtp.example.org"
sender = "a@example.org"
use_tls = true
use_ssl = true
"""
    path = _write(tmp_path, bad)
    with pytest.raises(ConfigError):
        load_config(path, env={})


# --- Credential safety: never logged, never echoed, never in exceptions ---


def test_password_never_appears_in_repr_or_str():
    config = SmtpConfig(
        host="smtp.example.org", sender="a@example.org", password="top-secret-value"
    )
    assert "top-secret-value" not in repr(config)
    assert "top-secret-value" not in str(config)


def test_password_never_appears_in_safe_summary():
    config = SmtpConfig(
        host="smtp.example.org", sender="a@example.org", password="top-secret-value"
    )
    assert "top-secret-value" not in config.safe_summary()


def test_password_never_appears_in_config_error_for_bad_config(tmp_path):
    """A config that fails validation for an unrelated reason must not
    leak the password value into the ConfigError message."""
    bad = """
[smtp]
host = "smtp.example.org"
sender = "a@example.org"
password = "leak-me-not"
use_tls = true
use_ssl = true
"""
    path = _write(tmp_path, bad)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert "leak-me-not" not in str(excinfo.value)


def test_password_never_appears_in_config_error_when_toml_itself_is_invalid(tmp_path):
    path = _write(
        tmp_path,
        'password = "leak-me-not"\nthis is not [valid toml',
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert "leak-me-not" not in str(excinfo.value)
