"""CWE catalog + CweGuard pre/post self-checks."""
from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.security.catalog import get_catalog
from app.security.checks import build_checks
from app.security.cwe_guard import CweGuard, CweViolation
from app.security.step_map import load_step_map


def run(coro):
    return asyncio.run(coro)


# ── Catalog ─────────────────────────────────────────────────────────────────
def test_catalog_resolves_weakness():
    got = get_catalog().resolve("CWE-89")
    assert got["type"] == "weakness"
    assert "SQL" in got["name"].upper() or got["name"]


def test_catalog_distinguishes_view_from_weakness():
    # CWE-1000 is the Research view — must NOT resolve as a weakness.
    assert get_catalog().resolve("CWE-1000")["type"] == "view"
    assert get_catalog().is_weakness("CWE-1000") is False


def test_catalog_unknown_raises():
    with pytest.raises(KeyError):
        get_catalog().resolve("CWE-99999999")


# ── Guard ───────────────────────────────────────────────────────────────────
def _guard(allow="api.example.com") -> CweGuard:
    settings = Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40,
                        EGRESS_ALLOWED_HOSTS=allow)
    return CweGuard(get_catalog(), load_step_map(), build_checks(settings), enabled=True)


def test_egress_allows_listed_host():
    async def scenario():
        async with _guard().step("egress", url="https://api.example.com/v1/x"):
            pass
    run(scenario())  # no raise


def test_egress_blocks_unlisted_host():
    async def scenario():
        async with _guard().step("egress", url="https://evil.example.net/x"):
            pass
    with pytest.raises(CweViolation) as ei:
        run(scenario())
    assert ei.value.cwe_id == "CWE-918"


def test_file_write_blocks_traversal_key():
    async def scenario():
        async with _guard().step("file_write", key="../../etc/passwd"):
            pass
    with pytest.raises(CweViolation) as ei:
        run(scenario())
    assert ei.value.cwe_id == "CWE-22"


def test_file_write_allows_safe_key():
    async def scenario():
        async with _guard().step("file_write", key="dir/file.txt"):
            pass
    run(scenario())


def test_db_query_requires_parameterized():
    async def bad():
        async with _guard().step("db_query", parameterized=False):
            pass
    with pytest.raises(CweViolation) as ei:
        run(bad())
    assert ei.value.cwe_id == "CWE-89"

    async def good():
        async with _guard().step("db_query", parameterized=True):
            pass
    run(good())


def test_respond_post_flags_secret_leak():
    async def scenario():
        async with _guard().step("respond") as step:
            step.result = {"user": "a", "password": "leak"}
    with pytest.raises(CweViolation) as ei:
        run(scenario())
    assert ei.value.cwe_id == "CWE-200"


def test_respond_post_passes_clean_body():
    async def scenario():
        async with _guard().step("respond") as step:
            step.result = {"user": "a", "count": 3}
    run(scenario())


def test_unknown_step_is_noop():
    async def scenario():
        async with _guard().step("no_such_step", whatever=1):
            pass
    run(scenario())


def test_disabled_guard_skips_checks():
    settings = Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40)
    guard = CweGuard(get_catalog(), load_step_map(), build_checks(settings), enabled=False)

    async def scenario():
        async with guard.step("egress", url="https://evil.example.net/x"):
            pass
    run(scenario())  # disabled -> no enforcement


def test_report_resolves_cwe_names():
    report = _guard().report()
    steps = {r["step"] for r in report}
    assert "egress" in steps and "db_query" in steps
    egress = next(r for r in report if r["step"] == "egress")
    assert egress["cwe"][0]["id"] == "CWE-918"
    assert egress["cwe"][0]["type"] == "weakness"
