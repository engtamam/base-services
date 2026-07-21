"""SecretsProvider contract — env + file (both zero-infra)."""
from __future__ import annotations

import pytest

from app.adapters.secrets.env import EnvSecrets
from app.adapters.secrets.file import FileSecrets
from app.ports.errors import ConfigError


def test_env_get_and_require(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "s3cr3t")
    provider = EnvSecrets()
    assert provider.get("MY_SECRET") == "s3cr3t"
    assert provider.require("MY_SECRET") == "s3cr3t"


def test_env_missing_returns_none_and_require_raises(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    provider = EnvSecrets()
    assert provider.get("NOPE") is None
    with pytest.raises(ConfigError):
        provider.require("NOPE")


def test_file_reads_values(tmp_path):
    f = tmp_path / "secrets.env"
    f.write_text('API_KEY=abc123\n# comment\nQUOTED="q v"\n', encoding="utf-8")
    provider = FileSecrets(str(f))
    assert provider.get("API_KEY") == "abc123"
    assert provider.get("QUOTED") == "q v"


def test_file_env_wins(tmp_path, monkeypatch):
    f = tmp_path / "secrets.env"
    f.write_text("API_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("API_KEY", "from_env")
    provider = FileSecrets(str(f))
    assert provider.get("API_KEY") == "from_env"  # env overrides file


def test_file_missing_is_fail_open(tmp_path):
    provider = FileSecrets(str(tmp_path / "does-not-exist.env"))
    assert provider.get("ANY") is None
