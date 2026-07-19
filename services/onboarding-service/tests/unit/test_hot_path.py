"""Guards every service in this repo carries, applied to onboarding-service.

The knob-guard matters here for the same reason it does in verification: this service's behaviour
IS its config. A declared-and-never-read knob is not untidiness — an ONBOARDING_PROVISIONERS entry
nothing consumes means a platform that believes it provisions tenants and provisions none.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.config import Settings


def test_every_settings_field_is_consumed_somewhere():
    """A config knob nothing reads is a lie about what the deployment controls."""
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


def test_source_and_grant_config_fails_fast_on_nonsense():
    """Malformed config must fail at BOOT. Discovered at request time it surfaces as UNKNOWN
    requirements — which looks like an outage in the owning service and hides a typo in ours."""
    base = dict(INTERNAL_SERVICE_TOKEN="t" * 40)

    for bad in ("identity", "identity=", "=http://x", "identity=ftp://x"):
        with pytest.raises(ValueError):
            _ = Settings(**base, ONBOARDING_SOURCES=bad).onboarding_sources

    for bad in ("identity", "identity=facts", "=/path"):
        with pytest.raises(ValueError):
            _ = Settings(**base, ONBOARDING_SOURCE_PATHS=bad).onboarding_source_paths

    for bad in ("welcome", "welcome=amount", "welcome=amount:20.5", "=amount:1"):
        with pytest.raises(ValueError):
            _ = Settings(**base, ONBOARDING_GRANTS=bad).onboarding_grants


def test_money_must_be_integer_minor_units():
    """A float in a gift amount is a rounding bug in money."""
    with pytest.raises(ValueError, match="minor units"):
        _ = Settings(INTERNAL_SERVICE_TOKEN="t" * 40,
                     ONBOARDING_GRANTS="welcome=amount:20.5").onboarding_grants


def test_a_provisioner_without_an_undo_is_refused():
    """A create with no matching delete is exactly how orphan tenants happen."""
    base = dict(INTERNAL_SERVICE_TOKEN="t" * 40)
    with pytest.raises(ValueError, match="create_url|undo_url"):
        _ = Settings(**base, ONBOARDING_PROVISIONERS="tenant=http://x").onboarding_provisioners
    with pytest.raises(ValueError, match=r"\{id\}"):
        _ = Settings(**base,
                     ONBOARDING_PROVISIONERS="tenant=http://x|/gone").onboarding_provisioners


def test_production_refuses_a_flow_that_promises_a_grant_nothing_can_issue():
    """Otherwise every customer completes onboarding believing they received something, while the
    dashboard shows the feature enabled."""
    with pytest.raises(ValueError, match="ONBOARDING_ENTITLEMENT is null"):
        Settings(INTERNAL_SERVICE_TOKEN="t" * 40, ENV="production",
                 ONBOARDING_ENTITLEMENT="null").assert_onboarding_config_ok()


def test_dev_defaults_boot_with_zero_infrastructure():
    Settings(INTERNAL_SERVICE_TOKEN="t" * 40, ENV="test").assert_onboarding_config_ok()
