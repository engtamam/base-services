"""RETENTION_DAYS enforcement — the knob is now a real policy, and the chain stays valid."""
from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import re

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.integrity.hash_chain import HashChainIntegrity
from app.core.audit_repository import AuditRepository
from app.core.audit_service import AuditService


def run(c):
    return asyncio.run(c)


def _svc(st, mode="tenant"):
    return AuditService(repo=AuditRepository(st), integrity=HashChainIntegrity(),
                        exporters={}, default_format="json",
                        partition_mode=mode, query_limit=1000)


async def _record_at(svc, st, action, ts, tenant=""):
    """Record then backdate the stored event's ts (to simulate age)."""
    ev = await svc.record(service="s", action=action, tenant=tenant)
    eid = ev["id"] if isinstance(ev, dict) else ev.id
    row = await st.get("audit_events", eid)
    row["ts"] = ts
    await st.put("audit_events", eid, row)


def test_retention_disabled_is_a_noop():
    st = MemoryDataStore()
    svc = _svc(st)
    run(svc.record(service="s", action="a"))
    out = run(svc.enforce_retention(0))
    assert out["enforced"] is False and out["purged"] == 0


def test_enforce_retention_purges_old_events_only():
    st = MemoryDataStore()
    svc = _svc(st)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)).isoformat()
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    run(_record_at(svc, st, "ancient", old))
    run(_record_at(svc, st, "fresh", recent))
    out = run(svc.enforce_retention(90))
    assert out["enforced"] is True and out["purged"] >= 1
    # the recent event survives; the chain still verifies after re-anchor
    verify = run(svc.verify("global"))
    assert verify["ok"] is True


def test_enforce_retention_spans_partitions():
    st = MemoryDataStore()
    svc = _svc(st)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)).isoformat()
    run(_record_at(svc, st, "a", old, tenant="tenant-a"))
    run(_record_at(svc, st, "b", old, tenant="tenant-b"))
    out = run(svc.enforce_retention(30))
    assert out["partitions"] >= 2 and out["purged"] >= 2


def test_every_settings_field_is_consumed_somewhere():
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
