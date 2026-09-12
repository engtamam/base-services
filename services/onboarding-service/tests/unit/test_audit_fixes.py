"""Regression tests for onboarding-service audit findings #01, #03, #04, #05."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.local_facts import LocalFacts
from app.ports.requirement_source import SATISFIED


def run(c):
    return asyncio.run(c)


class _Clock:
    def now(self):
        return 1000.0


# ── 03 + 04: default-scope grant is satisfied AND carries tenant/subject for erasure ──
def test_default_scope_grant_is_satisfied_and_erasable():
    st = MemoryDataStore()
    lf = LocalFacts(st, _Clock())
    # issue() defaults scope to 'tenant'; mark with that default
    run(lf.mark_granted(requirement="welcome", tenant="t1", subject="u1", scope="tenant",
                        reference="grant_1"))
    # a check with NO explicit scope must now resolve to tenant (matching issue) and be satisfied
    ans = run(lf.check(fact="granted", tenant="t1", subject="u1", req={"id": "welcome"}))
    assert ans.status == SATISFIED
    # and the row carries tenant/subject so DSAR find({tenant,subject}) matches (erasure)
    rows, _ = run(st.find("onboarding_grants", {"tenant": "t1", "subject": "u1"}, limit=10))
    assert len(rows) == 1


# ── 01: the wallet adapter sends an absolute expires_at derived from days ─────
def test_wallet_adapter_sends_expiry_from_days(monkeypatch):
    from app.adapters.entitlement import wallet as walletmod
    from app.ports.entitlement import GrantRequest

    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return b'{"id": "g1"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=0):
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(walletmod.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(walletmod.time, "time", lambda: 1_000_000.0)
    adapter = walletmod.WalletEntitlement("http://wallet/internal/wallet/credit", "tok")
    req = GrantRequest(kind="welcome", tenant="t1", subject="u1", idempotency_key="k",
                       amount=5000, currency="SAR", days=90, metadata={})
    run(adapter.issue(req))
    import json
    body = json.loads(captured["body"])
    assert body["expires_at"] == 1_000_000.0 + 90 * 86400   # absolute instant, not None


# ── 05: requirement-check URL percent-encodes caller-influenced values ────────
def test_http_source_encodes_url_values(monkeypatch):
    from app.adapters.requirement_source import http as httpmod

    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return b'{"satisfied": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(httpmod.urllib.request, "urlopen", _fake_urlopen)
    src = httpmod.HttpRequirementSource("svc", "http://svc", "tok", "/check/{subject}")
    run(src.check(fact="granted", tenant="t1", subject="../../admin?x=1"))
    # the traversal/query characters are percent-encoded, not left to rewrite the target
    assert "../" not in captured["url"] and "?x=1" not in captured["url"]
    assert "%2F" in captured["url"] or "%2E" in captured["url"]
