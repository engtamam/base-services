"""Non-security MEDIUM remediation (assistant-service):
  009  the zero-usage metering floor estimates input from the REQUEST and output from the response.
  013  converse() returns a distinct 'empty' discriminant (with token counts) instead of raising, so a
       blank completion is still metered and not conflated with a transport failure.
  016  invoke() refuses a tools-declaring connection (its single-shot path cannot carry the tool contract).
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.model.http_provider import HttpModel
from app.core.connections import AiConnection, ConnectionError, invoke


def run(c):
    return asyncio.run(c)


# ── 009: input/output estimated separately when usage is omitted ──────────────
def test_usage_floor_estimates_input_and_output_separately():
    body = {"choices": [{"message": {"content": "ok"}}]}        # no usage block, tiny response
    big_request = {"messages": [{"role": "user", "content": "x" * 4000}]}   # large prompt
    _text, tin, tout = HttpModel._extract("chat", body, big_request)
    assert tin > tout                                          # input (big prompt) > output (tiny body)
    assert tin > 0 and tout > 0                                # never zero (unbilled)


def test_usage_floor_without_request_is_still_nonzero():
    body = {"choices": [{"message": {"content": "hello"}}]}
    _t, tin, tout = HttpModel._extract("chat", body)
    assert tin >= 1 and tout >= 1


# ── 013: empty completion is a distinct discriminant, not a raise ─────────────
def test_extract_converse_tool_call_has_no_text_and_is_not_empty():
    body = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "f", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
    out = HttpModel._extract_converse("chat", body)
    assert out["kind"] == "tool_call" and out["tokens_in"] == 3


# ── 016: invoke refuses a tools-declaring connection ──────────────────────────
def test_invoke_refuses_a_tools_connection():
    conn = AiConnection(id="c1", tenant="t", name="n", url="https://x",
                        tools=[{"name": "lookup", "parameters": {"type": "object", "properties": {}}}])

    async def _caller(system, prompt, max_tokens):
        return "should not be reached", 1, 1

    class _Store:
        async def record_usage(self, **k): pass

    with pytest.raises(ConnectionError):
        run(invoke(conn, prompt="hi", caller=_caller, store=_Store()))


def test_invoke_allows_a_tool_less_connection():
    conn = AiConnection(id="c1", tenant="t", name="n", url="https://x")
    recorded = {}

    async def _caller(system, prompt, max_tokens):
        return "answer", 2, 3

    class _Store:
        async def record_usage(self, **k): recorded.update(k)

    out = run(invoke(conn, prompt="hi", caller=_caller, store=_Store()))
    assert out["text"] == "answer" and recorded["tokens_in"] == 2
