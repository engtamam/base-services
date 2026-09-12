"""Regression tests for the 2026-09-12 fleet-audit findings on assistant-service (#02, #03, #04).

(#01 retention paging is covered in test_conversations.py::test_retention_purges_all_expired_across_pages.)
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.model.http_provider import HttpModel, current_tenant
from app.core import ssrf, support
from app.ports.model import ModelError


def run(c):
    return asyncio.run(c)


# ── 02: the key resolver is given the request-scoped tenant ───────────────────
def test_credential_passes_the_current_tenant_to_the_resolver():
    seen = {}

    async def resolver(tenant=""):
        seen["tenant"] = tenant
        return f"key-for-{tenant}"

    m = HttpModel("https://model.example/v1", "", key_resolver=resolver)
    tok = current_tenant.set("shopX")
    try:
        key = run(m._credential())
    finally:
        current_tenant.reset(tok)
    assert seen["tenant"] == "shopX"        # was always "" before — tenant key silently ignored
    assert key == "key-for-shopX"


# ── 03: an advisory triage/draft never raises on a provider failure ───────────
class _BoomModel:
    driver = "http"

    async def answer(self, **_kw):
        raise ModelError("provider down")


def test_triage_swallows_a_model_error():
    out = run(support.triage(_BoomModel(), current_type="q", subject="s", body="b",
                             candidate_types=("q", "bug")))
    assert out == {}                        # a shrug, not a 500


def test_draft_swallows_a_model_error():
    out = run(support.draft(_BoomModel(), subject="s", thread=[{"author": "u", "body": "hi"}]))
    assert out == {}


# ── 04: connection URL SSRF guard ─────────────────────────────────────────────
def test_ssrf_blocks_metadata_even_when_private_is_allowed():
    with pytest.raises(ssrf.SsrfBlocked):
        ssrf.validate("http://169.254.169.254/latest/meta-data/", require_https=False,
                      allow_private=True)


def test_ssrf_blocks_a_private_host_by_default():
    with pytest.raises(ssrf.SsrfBlocked):
        ssrf.validate("http://127.0.0.1/v1", require_https=False, allow_private=False)


def test_ssrf_allows_a_public_https_host():
    t = ssrf.validate("https://example.com/v1")   # resolves to a public IP
    assert t.host == "example.com"
