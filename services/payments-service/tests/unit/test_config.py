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


def test_every_settings_field_is_consumed_somewhere():
    """An unwired knob is a lie in the API surface — a panel accepts the change, the audit records it,
    and behaviour does not move because nothing reads the override (the advertised-but-unread trap).
    Every Settings field must be referenced somewhere in non-test source."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    files = [p for p in root.rglob("*") if p.is_file()
             and p.suffix in (".py", ".toml", "") and "tests" not in p.parts
             and ".venv" not in p.parts and "__pycache__" not in p.parts]
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
