"""The admin-catalog aggregator: config-only discovery, untrusted declarations, cap filtering."""
from __future__ import annotations

import asyncio

import pytest

from app.core.admin_catalog import (
    ServiceTarget,
    build_catalog,
    parse_targets,
    _caps_allow,
    _valid_action,
)


def run(c):
    return asyncio.run(c)


# ── discovery is config, and well-formed (T2) ────────────────────────────────
def test_parse_targets():
    t = parse_targets("tenant-service=http://tenant:8080, acme=https://acme:9000")
    assert t == [ServiceTarget("tenant-service", "http://tenant:8080"),
                 ServiceTarget("acme", "https://acme:9000")]


def test_parse_targets_refuses_a_bare_host():
    with pytest.raises(ValueError):
        parse_targets("tenant=tenant:8080")            # no scheme
    with pytest.raises(ValueError):
        parse_targets("tenant")                         # no url


# ── a declaration is untrusted (T4) ──────────────────────────────────────────
def test_a_relative_path_is_accepted():
    a = _valid_action({"id": "tenant.suspend", "label": "Suspend", "method": "POST",
                       "path": "/api/v1/tenants/{id}/suspend", "capability": "platform.tenants"})
    assert a and a["id"] == "tenant.suspend"


@pytest.mark.parametrize("bad_path", [
    "http://evil/x",            # absolute URL — SSRF/redirect
    "https://evil",             # scheme
    "//evil/x",                 # protocol-relative
    "/x?redirect=http://evil",  # query
    "ftp://evil",
])
def test_a_nonrelative_path_is_dropped(bad_path):
    assert _valid_action({"id": "x.y", "label": "L", "method": "GET",
                          "path": bad_path, "capability": "c.d"}) is None


def test_a_bad_method_is_dropped():
    assert _valid_action({"id": "x.y", "label": "L", "method": "CONNECT",
                          "path": "/x", "capability": "c.d"}) is None


def test_a_hostile_label_is_kept_but_the_renderer_escapes_it():
    # The aggregator keeps the label (the renderer escapes); it must not crash on it.
    a = _valid_action({"id": "x.y", "label": "<script>alert(1)</script>", "method": "GET",
                       "path": "/x", "capability": "c.d"})
    assert a["label"] == "<script>alert(1)</script>"


def test_danger_implies_confirm():
    a = _valid_action({"id": "x.y", "label": "L", "method": "DELETE", "path": "/x",
                       "capability": "c.d", "danger": True})
    assert a["danger"] is True and a["confirm"] is True


# ── capability filtering (T5) ────────────────────────────────────────────────
def test_caps_allow_exact_and_wildcard():
    assert _caps_allow(frozenset({"platform.tenants"}), "platform.tenants")
    assert _caps_allow(frozenset({"platform.*"}), "platform.tenants")
    assert _caps_allow(frozenset({"*"}), "anything.at.all")
    assert not _caps_allow(frozenset({"billing.manage"}), "platform.tenants")


def _manifest(actions):
    return {"admin_actions": actions}


def test_catalog_returns_only_actions_the_caller_can_use():
    targets = [ServiceTarget("tenant-service", "http://t")]
    actions = [
        {"id": "a.1", "label": "A", "method": "GET", "path": "/a", "capability": "platform.tenants"},
        {"id": "b.1", "label": "B", "method": "GET", "path": "/b", "capability": "platform.billing"},
    ]

    async def fetch(_):
        return _manifest(actions)

    got = run(build_catalog(targets, frozenset({"platform.tenants"}), fetch))
    ids = [a["id"] for s in got["services"] for a in s["actions"]]
    assert ids == ["a.1"], "a caller without platform.billing must not see the billing action"


def test_a_no_cap_caller_gets_an_empty_catalog():
    targets = [ServiceTarget("t", "http://t")]

    async def fetch(_):
        return _manifest([{"id": "a.1", "label": "A", "method": "GET", "path": "/a",
                           "capability": "platform.tenants"}])

    got = run(build_catalog(targets, frozenset(), fetch))
    assert got["services"] == []


def test_an_unreachable_service_is_reported_not_dropped():
    targets = [ServiceTarget("down", "http://down")]

    async def fetch(_):
        raise ConnectionError("boom")

    got = run(build_catalog(targets, frozenset({"*"}), fetch))
    assert got["services"] == [] and got["unreachable"] == ["down"]


def test_a_hostile_manifest_cannot_inject_an_absolute_url():
    """A customer's service returns a manifest that tries to point an action at an attacker host.
    The aggregator drops it — the action never reaches the panel."""
    targets = [ServiceTarget("evil", "http://evil")]

    async def fetch(_):
        return _manifest([{"id": "e.1", "label": "Pwn", "method": "POST",
                           "path": "http://attacker/steal", "capability": "*"}])

    got = run(build_catalog(targets, frozenset({"*"}), fetch))
    assert got["services"] == [], "an absolute-URL action must be refused"


# ── the fan-out is CONCURRENT + bounded (login-blocking perf bug, fixed) ──────
def test_fan_out_runs_concurrently_not_sequentially():
    """The bug: `for t: await fetch(t)` made the wait the SUM of every upstream call, so a login
    (which blocks on this catalog) hung for over a minute with 12+ services. gather makes it the
    slowest SINGLE call. We assert wall-clock ≈ one delay, not N delays."""
    import time
    targets = [ServiceTarget(f"s{i}", f"http://s{i}") for i in range(10)]

    async def fetch(t):
        await asyncio.sleep(0.05)          # each "upstream" takes 50ms
        return _manifest([{"id": f"{t.name}.a", "label": "A", "method": "GET", "path": "/a", "capability": "platform.x"}])

    start = time.monotonic()
    got = run(build_catalog(targets, frozenset({"*"}), fetch))
    elapsed = time.monotonic() - start
    assert len(got["services"]) == 10                 # all fetched
    # sequential would be ~0.5s (10 x 50ms); concurrent is ~0.05s. Generous bound rules out serial.
    assert elapsed < 0.30, f"fan-out looks sequential ({elapsed:.2f}s for 10 x 50ms)"


def test_a_hung_service_is_bounded_by_the_timeout_and_reported_unreachable():
    """One blackholed service must not stall the rest: it is bounded by timeout_seconds and reported
    unreachable, while healthy services still return."""
    import time
    targets = [ServiceTarget("hung", "http://hung"), ServiceTarget("ok", "http://ok")]

    async def fetch(t):
        if t.name == "hung":
            await asyncio.sleep(5)         # would blackhole the whole catalog without a timeout
        return _manifest([{"id": f"{t.name}.a", "label": "A", "method": "GET", "path": "/a", "capability": "platform.x"}])

    start = time.monotonic()
    got = run(build_catalog(targets, frozenset({"*"}), fetch, timeout_seconds=0.1))
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, "a hung service was not bounded by the timeout"
    assert got["unreachable"] == ["hung"]
    assert [s["service"] for s in got["services"]] == ["ok"]   # healthy one still returned
