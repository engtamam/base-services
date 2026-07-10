"""Config is fail-fast and rejects weak/placeholder secrets."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def _make(**overrides):
    # _env_file=None so a stray .env never interferes with the test.
    return Settings(_env_file=None, **overrides)


def test_missing_internal_token_fails_fast():
    with pytest.raises(ValidationError):
        _make(INTERNAL_SERVICE_TOKEN=None)  # type: ignore[arg-type]


def test_short_internal_token_rejected():
    with pytest.raises(ValidationError):
        _make(INTERNAL_SERVICE_TOKEN="tooshort")


def test_placeholder_internal_token_rejected():
    with pytest.raises(ValidationError):
        _make(INTERNAL_SERVICE_TOKEN="PLACEHOLDER_CHANGE_ME_MIN_32_CHARS____")


def test_valid_config_ok():
    s = _make(INTERNAL_SERVICE_TOKEN="a" * 40, ENV="test")
    assert s.is_test is True
    assert s.is_production is False


# ── the config-knob guard (required of every service; see tools/ci/check_service_coverage.py)
# A knob that nothing reads is documented, configurable, and inert. feature-flag, webhook,
# search and connector each shipped one before this guard existed.
def test_every_settings_field_is_consumed_somewhere():
    import pathlib
    import re

    from app.config import Settings

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


# ── FATOORA_ENV: found by the coverage meta-gate as a knob nothing consumed ──
def test_fatoora_env_rejects_a_typo_rather_than_silently_downgrading():
    """"prod" must not quietly resolve to "not production" — that downgrades a live
    tax integration into a sandbox one, and nobody notices until the audit."""
    import pytest
    from app.config import Settings

    for bad in ("prod", "PRODUCTIONN", "live", ""):
        with pytest.raises(Exception):
            Settings(INTERNAL_SERVICE_TOKEN="t" * 40, FATOORA_ENV=bad)


def test_fatoora_env_is_normalised_and_accepted():
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="t" * 40, FATOORA_ENV="  PRODUCTION ")
    assert s.FATOORA_ENV == "production" and s.is_fatoora_production is True
    assert Settings(INTERNAL_SERVICE_TOKEN="t" * 40).is_fatoora_production is False


def test_declaring_production_with_a_fail_closed_client_refuses_to_boot():
    """A pod that believes it clears invoices and silently clears nothing is a
    compliance failure discovered at audit, not at deploy."""
    import pytest
    from app.config import Settings
    from app.container import build_container
    from app.ports.errors import ConfigError

    s = Settings(INTERNAL_SERVICE_TOKEN="t" * 40, FATOORA_ENV="production",
                 FATOORA_CLIENT_DRIVER="null")
    with pytest.raises(ConfigError, match="silently clear nothing"):
        build_container(s)


def test_declaring_production_without_a_base_url_refuses_to_boot():
    import pytest
    from app.config import Settings
    from app.container import build_container
    from app.ports.errors import ConfigError

    s = Settings(INTERNAL_SERVICE_TOKEN="t" * 40, FATOORA_ENV="production",
                 FATOORA_CLIENT_DRIVER="direct", FATOORA_BASE_URL="")
    with pytest.raises(ConfigError, match="points nowhere"):
        build_container(s)


def test_sandbox_with_a_real_url_is_allowed():
    """The inverse is safe and common: testing against a real sandbox endpoint."""
    from app.config import Settings
    from app.container import build_container

    s = Settings(INTERNAL_SERVICE_TOKEN="t" * 40, FATOORA_ENV="sandbox",
                 FATOORA_CLIENT_DRIVER="direct", FATOORA_BASE_URL="https://sandbox.example")
    assert build_container(s) is not None
