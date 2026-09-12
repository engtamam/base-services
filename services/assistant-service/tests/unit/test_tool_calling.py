"""Per-connection tool/function-calling for AI connections — both wire shapes.

The transport is injected, so these describe the WIRE CONTRACT verified against the providers'
current first-party docs (2026-09): OpenAI Chat Completions `tools`/`tool_calls` and Gemini
generateContent `functionDeclarations`/`functionCall`. Nothing here talks to a live provider.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.model.http_provider import CHAT, GENERATE, HttpModel

TOOLS = [{"name": "check_availability", "description": "Free slots",
          "parameters": {"type": "object", "properties": {"day": {"type": "string"}},
                         "required": ["day"]}}]


def run(c):
    return asyncio.run(c)


def _model(body, *, shape=CHAT, capture=None, status=200, **kw):
    async def transport(url, headers, payload, timeout):
        if capture is not None:
            capture.update(url=url, headers=headers, payload=payload)
        return status, body
    return HttpModel("https://p/v1", "k", shape=shape, transport=transport, **kw)


# ── chat shape ──────────────────────────────────────────────────────────────

def test_chat_tools_are_declared_in_the_request():
    cap = {}
    m = _model({"choices": [{"message": {"content": "hi"}}]}, capture=cap)
    run(m.converse(system="s", messages=[{"role": "user", "content": "hey"}], tools=TOOLS))
    tools = cap["payload"]["tools"]
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "check_availability"
    assert tools[0]["function"]["parameters"]["required"] == ["day"]


def test_chat_a_tool_call_is_returned_with_parsed_arguments():
    body = {"choices": [{"message": {"tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "check_availability", "arguments": '{"day": "tuesday"}'}}]}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4}}
    out = run(_model(body).converse(system="s", messages=[{"role": "user", "content": "x"}],
                                    tools=TOOLS))
    assert out["kind"] == "tool_call"
    assert out["calls"][0] == {"id": "call_1", "name": "check_availability",
                               "arguments": {"day": "tuesday"}}
    assert out["tokens_in"] == 12 and out["tokens_out"] == 4


def test_chat_a_plain_text_answer_is_returned():
    out = run(_model({"choices": [{"message": {"content": "all good"}}]}).converse(
        system="s", messages=[{"role": "user", "content": "x"}], tools=TOOLS))
    assert out["kind"] == "text" and out["text"] == "all good"
    # usage omitted → estimated floor, never zero (assistant-r2-02)
    assert out["tokens_in"] > 0 and out["tokens_out"] > 0


def test_chat_the_transcript_maps_assistant_toolcalls_and_tool_results():
    cap = {}
    m = _model({"choices": [{"message": {"content": "done"}}]}, capture=cap)
    transcript = [
        {"role": "user", "content": "book tuesday"},
        {"role": "assistant", "tool_calls": [
            {"id": "call_1", "name": "check_availability", "arguments": {"day": "tuesday"}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "check_availability",
         "content": "3 slots"},
    ]
    run(m.converse(system="s", messages=transcript, tools=TOOLS))
    msgs = cap["payload"]["messages"]
    assert msgs[0]["role"] == "system"
    asst = msgs[2]
    assert asst["role"] == "assistant"
    # arguments must go on the wire as a JSON STRING (OpenAI's contract), not a dict
    assert asst["tool_calls"][0]["function"]["arguments"] == '{"day": "tuesday"}'
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": "3 slots"}


# ── generate (Gemini) shape ─────────────────────────────────────────────────

def test_generate_tools_are_declared_as_functiondeclarations():
    cap = {}
    m = _model({"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}, shape=GENERATE, capture=cap)
    run(m.converse(system="s", messages=[{"role": "user", "content": "hey"}], tools=TOOLS))
    decls = cap["payload"]["tools"][0]["functionDeclarations"]
    assert decls[0]["name"] == "check_availability"


def test_generate_a_functioncall_is_returned():
    body = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "check_availability", "id": "fc1", "args": {"day": "wed"}}}]}}],
        "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 2}}
    out = run(_model(body, shape=GENERATE).converse(system="s",
              messages=[{"role": "user", "content": "x"}], tools=TOOLS))
    assert out["kind"] == "tool_call"
    assert out["calls"][0] == {"id": "fc1", "name": "check_availability", "arguments": {"day": "wed"}}


def test_generate_maps_toolcall_and_result_turns_to_parts():
    cap = {}
    m = _model({"candidates": [{"content": {"parts": [{"text": "done"}]}}]}, shape=GENERATE, capture=cap)
    transcript = [
        {"role": "user", "content": "book wed"},
        {"role": "assistant", "tool_calls": [
            {"id": "fc1", "name": "check_availability", "arguments": {"day": "wed"}}]},
        {"role": "tool", "tool_call_id": "fc1", "name": "check_availability", "content": "2 slots"},
    ]
    run(m.converse(system="s", messages=transcript, tools=TOOLS))
    contents = cap["payload"]["contents"]
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["functionCall"] == {"name": "check_availability",
                                                       "args": {"day": "wed"}}
    fr = contents[2]["parts"][0]["functionResponse"]
    assert contents[2]["role"] == "user"
    assert fr["name"] == "check_availability" and fr["response"] == {"result": "2 slots"}
    assert fr["id"] == "fc1"


# ── a bare string transcript is the old single-shot shape ────────────────────

def test_a_plain_string_still_works_via_the_normalizer():
    from app.core.connections import _normalize_messages
    assert _normalize_messages("hello") == [{"role": "user", "content": "hello"}]
