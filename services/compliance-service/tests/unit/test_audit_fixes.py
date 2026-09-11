"""Regression tests for the 2026-09-12 fleet-audit findings on compliance-service.

Each would FAIL on the pre-fix code:
  01  DSAR erasure deletes consent under the CANONICAL (normalised) key, so a mixed-case purpose is
      actually removed (not left behind while the ack claims success).
  02  the consent sweeper writes its lapse flag under the canonical key (no nightly re-announce, no
      duplicate legacy row).
  03  restore-as-clone rewrites a composite id's tenant prefix, so consent/legal rows land under the
      destination tenant.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.backup import export, import_archive, make_sealer
from app.core.consent_sweeper import ConsentSweeper
from app.core.erasure import ComplianceErasure

_DAY = 86400.0


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def now(self):
        return self.t


class _Bus:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event, payload):
        self.published.append((event, payload))


def _put_consent(st, tenant, subject, raw_purpose, **extra):
    # Stored the way ConsentStore does: canonical (normalised) key, id == key, RAW purpose in the field.
    norm = raw_purpose.strip().casefold()
    key = f"{tenant}::{subject}::{norm}"
    row = {"id": key, "tenant": tenant, "subject": subject, "purpose": raw_purpose,
           "basis": "guardian", "granted": True, **extra}
    run(st.put("consent", key, row))
    return key


# ── 01: erasure removes a mixed-case-purpose consent ──────────────────────────
def test_erasure_deletes_a_mixed_case_purpose_consent():
    st = MemoryDataStore()
    key = _put_consent(st, "acme", "u1", "Marketing")     # canonical key is ...::marketing
    out = run(ComplianceErasure(store=st, pii_salt="pepper").erase("acme", "u1"))
    assert "consent:1" in out["erased"]
    assert run(st.get("consent", key)) is None            # actually gone (pre-fix left it behind)


# ── 02: the lapse flag lands on the canonical row, no duplicate, no re-announce ─
def test_sweeper_flags_the_canonical_row_and_does_not_reannounce():
    st = MemoryDataStore()
    clock = _Clock()
    bus = _Bus()
    canon = _put_consent(st, "acme", "kid@x.com", "Coaching",
                         expires_at=clock.now() + 10 * _DAY)   # inside the 30-day notice window
    sweeper = ConsentSweeper(store=st, consents=None, clock=clock, event_bus=bus,
                             notice_seconds=30 * _DAY, max_per_run=1000)
    run(sweeper.run())
    assert [e for e, _ in bus.published] == ["consent.lapsing"]
    assert run(st.get("consent", canon))["lapsing_announced"] is True   # flag on the canonical row
    assert run(st.get("consent", "acme::kid@x.com::Coaching")) is None  # no raw-key duplicate
    # a second run must NOT re-announce (the canonical row is already flagged)
    run(sweeper.run())
    assert [e for e, _ in bus.published] == ["consent.lapsing"]


# ── 03: restore-as-clone lands consent under the destination tenant ───────────
def test_clone_restore_remaps_a_composite_id_tenant_prefix():
    decl = {"collections": [{"name": "consent", "tenant_scoped": True}]}
    seal, unseal = make_sealer("backup-kek-123")
    src = MemoryDataStore()
    _put_consent(src, "acme", "u1", "Marketing")
    arch = run(export(src, decl, service="compliance", version="1", taken_at="t",
                      seal=seal, tenant="acme", scan_cap=1000))
    dst = MemoryDataStore()
    res = run(import_archive(dst, arch, unseal=unseal, into_tenant="beta", declaration=decl))
    assert res["inserted"] == 1 and res["skipped"] == 0
    assert run(dst.get("consent", "beta::u1::marketing")) is not None   # under the NEW tenant
    assert run(dst.get("consent", "acme::u1::marketing")) is None       # not the original key
