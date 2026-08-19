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
    assert rules[0].max_per_run == 40  # safe default, not None/unlimited


def test_max_per_run_zero_means_deliberately_unlimited(tmp_path):
    toml = BASIC_TOML + "\nmax_per_run = 0\n"
    path = _write(tmp_path, toml)
    config = load_config(path, env={})
    assert config.rule_configs()[0].max_per_run is None


def test_max_per_run_negative_rejected(tmp_path):
    toml = BASIC_TOML + "\nmax_per_run = -1\n"
    path = _write(tmp_path, toml)
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_max_per_run_explicit_value_is_respected(tmp_path):
    toml = BASIC_TOML + "\nmax_per_run = 5\n"
    path = _write(tmp_path, toml)
    config = load_config(path, env={})
    assert config.rule_configs()[0].max_per_run == 5


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


# --- run_log section: wired through, not silently discarded ---


def test_run_log_path_is_honoured(tmp_path):
    toml = BASIC_TOML + '\n[run_log]\npath = "/some/where/runs.jsonl"\n'
    path = _write(tmp_path, toml)
    config = load_config(path, env={})
    assert config.run_log.path == "/some/where/runs.jsonl"


def test_run_log_max_entries_is_honoured(tmp_path):
    toml = BASIC_TOML + "\n[run_log]\nmax_entries = 25\n"
    path = _write(tmp_path, toml)
    config = load_config(path, env={})
    assert config.run_log.max_entries == 25


# --- Strict validation: unknown keys are rejected, not silently defaulted ---


def test_unknown_top_level_section_rejected(tmp_path):
    toml = BASIC_TOML + '\n[bogus]\nfoo = "bar"\n'
    path = _write(tmp_path, toml)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert "bogus" in str(excinfo.value)


def test_unknown_key_in_smtp_rejected(tmp_path):
    bad = BASIC_TOML.replace(
        'sender = "alerts@example.org"',
        'sender = "alerts@example.org"\nsmtp_typo_key = "oops"',
        1,
    )
    path = _write(tmp_path, bad)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert "smtp_typo_key" in str(excinfo.value)


def test_unknown_key_in_state_rejected(tmp_path):
    toml = BASIC_TOML + '\n[state]\npathh = "typo.json"\n'
    path = _write(tmp_path, toml)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert "pathh" in str(excinfo.value)


def test_unknown_key_in_run_log_rejected(tmp_path):
    toml = BASIC_TOML + "\n[run_log]\nmax_entires = 25\n"
    path = _write(tmp_path, toml)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert "max_entires" in str(excinfo.value)


def test_unknown_key_in_rule_rejected(tmp_path):
    bad = BASIC_TOML + '\nseverty_at_least = "Severe"\n'
    path = _write(tmp_path, bad)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert "severty_at_least" in str(excinfo.value)


def test_bad_key_alongside_password_does_not_leak_password(tmp_path):
    """A typo elsewhere in the file must not cause the password to show
    up in the resulting error, even though the password is present and
    valid in the same document."""
    bad = BASIC_TOML + '\n[state]\npathh = "typo.json"\n'
    path = _write(tmp_path, bad)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert "super-secret-password" not in str(excinfo.value)


def test_password_never_appears_when_password_key_itself_is_typoed(tmp_path):
    """A typo on the password key's own name (passwrd instead of
    password) must not leak the value the operator wrote for it -- the
    extra_forbidden error otherwise echoes exactly that value."""
    bad = """
[smtp]
host = "smtp.example.org"
sender = "a@example.org"
passwrd = "typoed-secret-value"

[[rules]]
name = "r"
to = ["ops@example.org"]
"""
    path = _write(tmp_path, bad)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})
    assert "typoed-secret-value" not in str(excinfo.value)
    assert "passwrd" in str(excinfo.value)
