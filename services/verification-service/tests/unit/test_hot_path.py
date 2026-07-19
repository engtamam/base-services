"""Guards every service in this repo carries, applied to verification-service.

The knob-guard is the one that matters most here. This service's whole surface is config —
providers, profiles, credentials, TTLs — so a knob declared and never read is not a cosmetic
tidiness problem: it is a setting an operator writes, deploys, and believes is in force. A
`VERIFY_TWO_EYES` nobody consumes means a platform that thinks it requires two approvers and
accepts one.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.config import Settings


def test_every_settings_field_is_consumed_somewhere():
    """A config knob nothing reads is a lie about what the deployment controls.

    Scans the service's own source (never tests) for each declared field. The declaration line
    itself does not count as a use — otherwise every field trivially passes.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".toml", "")
             and "tests" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts]
    lines: list[str] = []
    for p in files:
        try:
            lines.extend(p.read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            continue

    unused = []
    for name in Settings.model_fields:
        decl = re.compile(rf"^\s*{re.escape(name)}\s*:")
        if not [ln for ln in lines if re.search(rf"\b{re.escape(name)}\b", ln) and not decl.match(ln)]:
            unused.append(name)
    assert not unused, f"config knobs declared but never consumed: {unused}"


def test_provider_config_fails_fast_on_nonsense():
    """A malformed provider map must fail at BOOT, not on the first customer who tries to verify.

    Discovering it at request time means the failure surfaces as an error verdict — which looks
    like a provider outage, sends someone to check the provider, and hides a typo in our own config.
    """
    base = dict(INTERNAL_SERVICE_TOKEN="t" * 40)

    for bad in ("business", "business=", "=wathq"):
        with pytest.raises(ValueError):
            _ = Settings(**base, VERIFY_PROVIDERS=bad).verify_providers

    for bad in ("wathq.url", "wathqurl=https://x", "=value"):
        with pytest.raises(ValueError):
            _ = Settings(**base, VERIFY_PROFILES=bad).verify_profiles

    for bad in ("wathq", "wathq=", "wathq=https://token.example"):
        with pytest.raises(ValueError):
            _ = Settings(**base, VERIFY_SECRETS=bad).verify_secrets

    for bad in ("business=forever", "business=-1", "business"):
        with pytest.raises(ValueError):
            _ = Settings(**base, VERIFY_TTL_DAYS=bad).verify_ttl_days


def test_a_credential_cannot_be_pasted_where_a_secret_name_belongs():
    """The parser refuses a value that looks like a secret rather than a name — a credential in a
    config string ends up in a ConfigMap, then a log line, then a support ticket."""
    base = dict(INTERNAL_SERVICE_TOKEN="t" * 40)
    with pytest.raises(ValueError, match="secret NAME"):
        _ = Settings(**base, VERIFY_SECRETS="wathq=https://api.example/token").verify_secrets


def test_production_refuses_a_provider_it_cannot_read_an_answer_from():
    """Without verdict_path we would not know where the answer lives, so every response would read
    as error while the dashboard showed the feature enabled."""
    base = dict(INTERNAL_SERVICE_TOKEN="t" * 40, ENV="production")
    with pytest.raises(ValueError, match="verdict_path"):
        Settings(**base, VERIFY_PROVIDERS="business=wathq",
                 VERIFY_PROFILES="wathq.url=https://api.example/cr").assert_verify_config_ok()


def test_production_refuses_the_null_verifier():
    base = dict(INTERNAL_SERVICE_TOKEN="t" * 40, ENV="production")
    with pytest.raises(ValueError, match="null verifier"):
        Settings(**base, VERIFY_PROVIDERS="business=null").assert_verify_config_ok()


def test_manual_review_needs_no_url():
    """A human answers it, so requiring an endpoint would block the one provider that works in
    every market with no external contract at all."""
    base = dict(INTERNAL_SERVICE_TOKEN="t" * 40, ENV="production")
    Settings(**base, VERIFY_PROVIDERS="business=manual").assert_verify_config_ok()


def test_dev_defaults_boot_with_zero_infrastructure():
    Settings(INTERNAL_SERVICE_TOKEN="t" * 40, ENV="test").assert_verify_config_ok()
