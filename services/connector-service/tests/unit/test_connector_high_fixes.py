"""connector-001/002/004 regressions: provider non-2xx is a failure (not silent success/empty page),
and unseal keeps a legacy no-AAD fallback."""
from __future__ import annotations

import pytest

import app.adapters.provider.oauth2_rest as o2
from app.core import seal as S
from app.core.seal import token_aad
from app.ports.connector_provider import ProviderUnavailable

_SPEC = {"base_url": "https://api.test", "endpoints": {"c": {"path": "/c"}}}


def test_fetch_raises_on_provider_4xx(monkeypatch):
    # connector-002: a 400/404/409 must NOT become an empty page that advances the watermark.
    monkeypatch.setattr(o2, "get_json", lambda *a, **k: (404, {}, {}))
    with pytest.raises(ProviderUnavailable):
        o2.OAuth2RestConnector().fetch(access_token="t", resource="c", cursor="", limit=10, spec=_SPEC)


def test_push_raises_on_provider_4xx(monkeypatch):
    # connector-001: a rejected write (400/409) must NOT be reported as a success.
    monkeypatch.setattr(o2, "post_json", lambda *a, **k: (409, {"error": "conflict"}, {}))
    with pytest.raises(ProviderUnavailable):
        o2.OAuth2RestConnector().push(access_token="t", resource="c", record={"id": "1"},
                                      idempotency_key="k1", spec=_SPEC)


def test_fetch_succeeds_on_2xx(monkeypatch):
    monkeypatch.setattr(o2, "get_json",
                        lambda *a, **k: (200, {"results": [{"id": "1"}], "next": "N"}, {}))
    spec = {"base_url": "https://api.test",
            "endpoints": {"c": {"path": "/c", "records_path": "results", "next_cursor_path": "next"}}}
    page = o2.OAuth2RestConnector().fetch(access_token="t", resource="c", cursor="", limit=10, spec=spec)
    assert [r["id"] for r in page.records] == ["1"] and page.next_cursor == "N"


# ── connector-004: legacy no-AAD blob still unseals after AAD binding was introduced ────────────────
def test_legacy_no_aad_blob_still_unseals():
    key = "seal-key"
    legacy = S.seal("s3cr3t", key, "")                     # sealed BEFORE aad binding (aad=None)
    # a bound read still recovers it via the legacy fallback
    assert S.unseal(legacy, key, token_aad("cn_1", "access")) == "s3cr3t"
    # a wrong key still fails (returns None), never a silent wrong value
    assert S.unseal(legacy, "wrong-key", token_aad("cn_1", "access")) is None


def test_aad_bound_blob_is_slot_bound():
    key = "seal-key"
    blob = S.seal("s3cr3t", key, token_aad("cn_1", "access"))
    assert S.unseal(blob, key, token_aad("cn_1", "access")) == "s3cr3t"
    assert S.unseal(blob, key, token_aad("cn_OTHER", "access")) is None   # relocated → refused
