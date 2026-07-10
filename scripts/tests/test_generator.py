"""Unit tests for the service generator (pure helpers + dry-run safety)."""
from __future__ import annotations

import pytest

import create_service as gen


# ── Name validation ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["auth-service", "identity", "a1", "x-y-z9"])
def test_valid_names(name):
    assert gen.validate_name(name) == name


@pytest.mark.parametrize("name", [
    "../evil", "a/b", "a\\b", "..", "Bad-Case", "-lead", "trail-", "x", "9lead", "a..b",
])
def test_invalid_names(name):
    with pytest.raises(gen.GenError):
        gen.validate_name(name)


# ── Driver validation ───────────────────────────────────────────────────────
def test_valid_driver():
    assert gen.validate_driver("cache", "redis") == "redis"


def test_invalid_driver():
    with pytest.raises(gen.GenError):
        gen.validate_driver("cache", "memcached")


# ── Placeholder engine ──────────────────────────────────────────────────────
def test_replace_placeholders():
    out = gen.replace_placeholders("name={service-name} pkg=service-template", "my-svc")
    assert out == "name=my-svc pkg=my-svc"


def test_cwe_json_not_replaced():
    assert gen.should_replace("app/security/cwe.json") is False
    assert gen.should_replace("app/config.py") is True


# ── Port assignment ─────────────────────────────────────────────────────────
def test_assign_port_requested():
    assert gen.assign_port({"a": 8001}, "b", 9000) == 9000


def test_assign_port_auto_skips_used():
    assert gen.assign_port({"a": 8001, "b": 8002}, "c", None) == 8003


# ── Config resolution (flags override spec) ─────────────────────────────────
def test_flags_override_spec():
    args = gen.build_parser().parse_args(["svc", "--cache", "redis"])
    spec = {"name": "spec-name", "port": 8500, "drivers": {"cache": "memory", "queue": "redis"}}
    cfg = gen.resolve_config(args, spec)
    assert cfg["name"] == "svc"          # positional overrides spec
    assert cfg["drivers"]["CACHE_DRIVER"] == "redis"   # flag overrides spec
    assert cfg["drivers"]["QUEUE_DRIVER"] == "redis"   # spec-only kept


# ── .env / spec generation ──────────────────────────────────────────────────
def test_env_activates_safe_defers_infra():
    env, deferred = gen.build_env_file(
        "svc", 8001, {"OBJECTSTORE_DRIVER": "memory", "CACHE_DRIVER": "redis"}
    )
    assert "OBJECTSTORE_DRIVER=memory" in env          # safe -> activated
    assert "# CACHE_DRIVER=redis" in env               # infra -> commented
    assert deferred == ["CACHE_DRIVER=redis"]
    assert "INTERNAL_SERVICE_TOKEN=" in env


def test_spec_file_contains_drivers():
    text = gen.build_spec_file("svc", "d", 8001, {"CACHE_DRIVER": "redis"})
    assert 'cache = "redis"' in text
    assert 'name = "svc"' in text


# ── Dry run writes nothing ──────────────────────────────────────────────────
def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "SERVICES", tmp_path)
    monkeypatch.setattr(gen, "REGISTRY", tmp_path / ".registry.json")
    rc = gen.main(["dry-svc", "--dry-run"])
    assert rc == 0
    assert not (tmp_path / "dry-svc").exists()
    assert not (tmp_path / ".registry.json").exists()
