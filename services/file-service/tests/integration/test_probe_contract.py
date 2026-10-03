"""The probe contract — what the Helm chart is allowed to point a probe at.

`livenessProbe` fires on every pod, every few seconds, forever. If it touches a dependency,
then one slow moment in Postgres restarts **every pod in the fleet at once** — a self-inflicted
outage caused by a single line of YAML. So:

    /healthz  (liveness)  — MUST do zero dependency work. Process alive? 200.
    /readyz   (readiness) — MUST probe the real dependencies. Deps down? 503, the pod leaves the
                            Service endpoints, and it is NOT restarted.

This file pins that contract in the service, so `deploy/chart` can rely on it. The chart has a
matching guard asserting it maps liveness->/healthz and readiness->/readyz, never the reverse.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def probe_app():
    """A throwaway app plus a clean readiness registry (the registry is a module global)."""
    from app.api import health as health_mod
    from app.config import get_settings

    saved_env = dict(os.environ)
    saved_checks = dict(health_mod._readiness_checks)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="t" * 40)
    get_settings.cache_clear()
    try:
        from app.main import create_app

        app = create_app()
        with TestClient(app) as client:
            yield app, client, health_mod
    finally:
        health_mod._readiness_checks.clear()
        health_mod._readiness_checks.update(saved_checks)
        os.environ.clear()
        os.environ.update(saved_env)
        get_settings.cache_clear()


def _count_probes(health_mod) -> dict[str, int]:
    """Wrap the REGISTERED readiness probes (bound at create_app) and count invocations."""
    counts: dict[str, int] = {}

    def wrap(name, probe):
        async def counted():
            counts[name] = counts.get(name, 0) + 1
            return await probe()

        return counted

    for name, probe in list(health_mod._readiness_checks.items()):
        counts.setdefault(name, 0)
        health_mod._readiness_checks[name] = wrap(name, probe)
    return counts


def test_healthz_touches_no_dependency(probe_app):
    """Liveness must be answerable by a pod whose database is on fire."""
    _, client, health_mod = probe_app
    counts = _count_probes(health_mod)

    for _ in range(50):
        assert client.get("/healthz").status_code == 200

    busy = {n: c for n, c in counts.items() if c}
    assert not busy, f"/healthz probed dependencies {busy} — a DB blip will restart every pod"


def test_healthz_is_cheap_and_unauthenticated(probe_app):
    _, client, _ = probe_app
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_readyz_actually_probes_every_wired_dependency(probe_app):
    """The mirror image: a readiness probe that checks nothing is equally useless."""
    app, client, health_mod = probe_app
    counts = _count_probes(health_mod)
    assert counts, "no readiness probes registered — /readyz would be a rubber stamp"

    assert client.get("/readyz").status_code == 200
    silent = [n for n, c in counts.items() if c == 0]
    assert not silent, f"/readyz did not probe {silent}"

    # Every adapter that CAN report health is actually registered.
    expected = set(app.state.container.health_probes())
    assert set(counts) == expected


def test_readyz_reports_503_when_a_dependency_is_down(probe_app):
    """Down deps must remove the pod from the Service, NOT kill it (that is liveness' job)."""
    _, client, health_mod = probe_app

    async def broken() -> bool:
        raise RuntimeError("down")

    health_mod.register_readiness_check("broken_dep", broken)

    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["broken_dep"] == "down"

    # ...and liveness is still fine, so Kubernetes will not restart the pod.
    assert client.get("/healthz").status_code == 200


def test_a_failing_probe_never_crashes_readyz(probe_app):
    _, client, health_mod = probe_app

    async def explode() -> bool:
        raise ValueError("boom")

    health_mod.register_readiness_check("explode", explode)
    assert client.get("/readyz").status_code == 503     # 503, not 500


def test_readyz_names_a_hanging_dependency_as_timeout_not_a_hang(probe_app, monkeypatch):
    """A blackholed dependency (egress that DROPS, a wedged upstream) makes a probe HANG, not fail.
    Unbounded, /readyz then never answers: the pod sits Running 0/1 with no body and nothing names
    the cause. The probe must bound each check and report the stuck one distinctly."""
    _, client, health_mod = probe_app
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "READYZ_TIMEOUT_SECONDS", 0.2)   # file-010: a real declared field

    async def hangs() -> bool:
        await asyncio.sleep(30)                 # never answers within the budget
        return True

    health_mod.register_readiness_check("stuck_dep", hangs)
    start = time.monotonic()
    r = client.get("/readyz")
    elapsed = time.monotonic() - start

    assert r.status_code == 503
    assert r.json()["checks"]["stuck_dep"] == "timeout"   # named, not a generic "down"
    assert elapsed < 5, "readyz hung on the check instead of bounding it"
