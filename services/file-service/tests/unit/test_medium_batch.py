"""Non-security MEDIUM remediation (file-service):
  005  the magic scanner rejects active content (SVG/HTML) by type AND by content, and fails closed on
       an unverifiable declared type in strict mode.
  008  find_stale stops at the first row past the cutoff (ascending created) instead of paging all rows.
  012  ScanWorker.run_once nacks a failing job and keeps draining the rest of the batch.
  019  assert_inline_scan_ok refuses an unscanned inline path in production without an explicit opt-out.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.scanner.magic import MagicScanner
from app.config import Settings
from app.ports.scanner import Rejected


def run(c):
    return asyncio.run(c)


# ── 005: active content + fail-closed ─────────────────────────────────────────
def test_svg_declared_type_is_rejected():
    with pytest.raises(Rejected):
        MagicScanner(strict=True).scan(b"<svg xmlns='...'><script>alert(1)</script></svg>",
                                       "image/svg+xml")


def test_html_bytes_under_an_image_type_is_rejected():
    # a .png that is really HTML must not slip through on its declared type
    with pytest.raises(Rejected):
        MagicScanner(strict=True).scan(b"<!DOCTYPE html><html><body>x", "image/png")


def test_unknown_declared_type_fails_closed_in_strict_mode():
    with pytest.raises(Rejected):
        MagicScanner(strict=True).scan(b"plain text here", "application/x-unknown")


def test_known_type_with_matching_bytes_passes():
    MagicScanner(strict=True).scan(b"\x89PNG\r\n\x1a\n rest of png", "image/png")   # no raise


def test_non_strict_allows_unknown_type():
    MagicScanner(strict=False).scan(b"plain text here", "application/x-unknown")    # no raise


# ── 008: early break on ascending created ─────────────────────────────────────
def test_find_stale_breaks_at_first_fresh_row():
    from app.adapters.datastore.memory import MemoryDataStore
    from app.core.upload_session import PENDING, UploadSession, UploadSessionStore

    st = MemoryDataStore()
    store = UploadSessionStore(st)
    for i, created in enumerate(("2020-01-01T00:00:00Z", "2020-06-01T00:00:00Z",
                                 "2999-01-01T00:00:00Z")):
        run(store.create(UploadSession(id=f"s{i}", tenant="t", owner="o", filename="f",
                                       content_type="application/octet-stream",
                                       declared_sha256="a" * 64, declared_size=1, upload_id="u",
                                       part_size=1, part_count=1, status=PENDING, created=created)))
    stale, cursor = run(store.find_stale([PENDING], "2025-01-01T00:00:00Z", limit=100))
    ids = {r["id"] for r in stale}
    assert ids == {"s0", "s1"}          # the two old ones
    assert cursor is None               # stopped at the fresh row; no further paging


# ── 012: run_once keeps draining past a failing job ───────────────────────────
class _Job:
    def __init__(self, sid): self.id = sid; self.queue = "q"; self.payload = {"session_id": sid}; self.token = "t"


class _Queue:
    def __init__(self, sids):
        self._pending = list(sids)
        self.nacked = []
        self.acked = []

    async def reclaim(self, q): return 0

    async def dequeue(self, q, *, lease_seconds):
        if not self._pending:
            return None
        return _Job(self._pending.pop(0))

    async def nack(self, job): self.nacked.append(job.id); return True
    async def ack(self, job): self.acked.append(job.id); return True


def test_run_once_continues_after_a_failing_job():
    from app.core.scan_worker import ScanWorker

    q = _Queue(["bad", "good"])
    sw = ScanWorker(files=None, sessions=None, object_store=None, scanner=None, queue=q,
                    lease_seconds=60)

    async def fake_process(sid):
        if sid == "bad":
            raise RuntimeError("boom")

    sw.process = fake_process
    processed = run(sw.run_once())
    assert processed == 1                 # "good" still processed
    assert q.nacked == ["bad"]            # the poison was nacked, not re-raised
    assert q.acked == ["good"]


# ── 019: inline-scan boot guard ───────────────────────────────────────────────
def _settings(**over):
    base = dict(_env_file=None, INTERNAL_SERVICE_TOKEN="x" * 40, ENV="production",
                PII_SALT="s" * 16, SCANNER_DRIVER="none")
    base.update(over)
    return Settings(**base)


def test_unscanned_inline_refused_in_production():
    with pytest.raises(ValueError):
        _settings().assert_inline_scan_ok()


def test_explicit_opt_out_allows_unscanned_inline():
    _settings(ALLOW_UNSCANNED_INLINE=True).assert_inline_scan_ok()   # no raise


def test_a_real_scanner_satisfies_the_guard():
    _settings(SCANNER_DRIVER="clamav").assert_inline_scan_ok()       # no raise
