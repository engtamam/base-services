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


def test_json_map_helpers_parse_a_nonempty_map_without_a_missing_import():
    # Regression: _json_any_map parses at MODULE scope with _json.loads, but the module-level
    # `import json as _json` had been dropped from THIS service's config.py — so every internal-token
    # route parsing a NON-EMPTY INTERNAL_CALLER_PUBLIC_KEYS_JSON crashed with NameError (config.py),
    # breaking require_internal_token fleet-wide on the affected services. The empty case early-returns
    # before _json.loads, which is why only a set pubkey map triggered it. Prove the real path works.
    from app.config import _json_str_map, _json_any_map
    assert _json_str_map('{"kid1": "pubkey-pem"}') == {"kid1": "pubkey-pem"}
    assert _json_any_map('{"a": 1}') == {"a": 1}
    assert _json_str_map("") == {} and _json_any_map("not json") == {}
