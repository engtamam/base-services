"""Round-2 fixes (assistant-service). Maps to docs/audits/round2/assistant-service.json."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.model.http_provider import HttpModel
from app.ports.model import ModelError


def run(c):
    return asyncio.run(c)


# r2-01 — an AI-connection URL is re-validated (DNS re-resolved) at DELIVERY, refusing a rebind to a
# private/blocked address before the credential-bearing POST is made.
def test_connection_url_is_revalidated_at_delivery():
    policy = {"require_https": False, "allow_private": False,
              "allowed_hosts": frozenset(), "denied_hosts": frozenset()}
    model = HttpModel("http://169.254.169.254/v1/chat", "k", model="m", shape="chat",
                      ssrf_revalidate=policy)
    with pytest.raises(ModelError):
        run(model.complete(system="s", user="u", max_tokens=16))


def test_no_ssrf_revalidate_uses_the_normal_path():
    # Without the connection policy (the main model path), no delivery-time SSRF check is imposed:
    # a custom transport is used as-is (config-sourced URL is already trusted).
    async def _transport(url, headers, payload, timeout):
        return 200, {"choices": [{"message": {"content": "ok [1]"}}], "usage":
                     {"prompt_tokens": 3, "completion_tokens": 2}}
    model = HttpModel("http://any.invalid/v1", "k", model="m", shape="chat", transport=_transport)
    text, _tin, _tout = run(model.complete(system="s", user="u", max_tokens=16))
    assert text


# r2-02 — a response without usage is metered to a non-zero floor, never free.
def test_missing_usage_is_never_billed_zero():
    async def _transport(url, headers, payload, timeout):
        return 200, {"choices": [{"message": {"content": "a real answer with no usage block"}}]}
    model = HttpModel("http://any.invalid/v1", "k", model="m", shape="chat", transport=_transport)
    _text, tin, tout = run(model.complete(system="s", user="u", max_tokens=16))
    assert tin > 0 and tout > 0
