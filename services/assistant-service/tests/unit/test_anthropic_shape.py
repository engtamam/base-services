"""The `anthropic` wire shape — Claude, direct API and Claude-on-Vertex (:rawPredict).

Transport is injected: these pin the WIRE CONTRACT verified against Anthropic + Google Vertex docs
(2026-09-09): Messages API — top-level `system`, messages[] user/assistant, `max_tokens` required,
response `content[].text` + `usage.input_tokens/output_tokens`; tools declared as
{name,description,input_schema} and returned as `tool_use` blocks; a tool result is a user turn with a
`tool_result` block. On Vertex, `anthropic_version` rides in the body and the model is named in the URL
(not the body), authed by a Bearer GCP token; direct API puts the model in the body and authenticates
with x-api-key + anthropic-version headers.
"""
from __future__ import annotations

import asyncio

from app.adapters.model.http_provider import ANTHROPIC, HttpModel

TOOLS = [{"name": "check_availability", "description": "Free slots",
          "parameters": {"type": "object", "properties": {"day": {"type": "string"}},
                         "required": ["day"]}}]


def run(c):
    return asyncio.run(c)


def _model(body, *, capture=None, status=200, **kw):
    async def transport(url, headers, payload, timeout):
        if capture is not None:
            capture.update(url=url, headers=headers, payload=payload)
        return status, body
    return HttpModel("https://p/v1", "k", shape=ANTHROPIC, transport=transport, **kw)


def test_direct_api_puts_model_in_body_and_uses_x_api_key():
    cap = {}
    m = _model({"content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 5, "output_tokens": 2}},
               capture=cap, model="claude-sonnet-4")
    txt, tin, tout = run(m.complete(system="s", user="hello", max_tokens=64))
    assert txt == "hi" and (tin, tout) == (5, 2)
    p = cap["payload"]
    assert p["system"] == "s" and p["messages"] == [{"role": "user", "content": "hello"}]
    assert p["model"] == "claude-sonnet-4" and p["max_tokens"] == 64
    assert "anthropic_version" not in p                       # direct API: not in the body
    assert cap["headers"]["x-api-key"] == "k" and "anthropic-version" in cap["headers"]
    assert "Authorization" not in cap["headers"]


def test_vertex_puts_version_in_body_model_in_url_and_uses_bearer():
    cap = {}
    m = _model({"content": [{"type": "text", "text": "ok"}]},
               capture=cap, model="claude", anthropic_version="vertex-2023-10-16")
    run(m.complete(system="s", user="hi"))
    p = cap["payload"]
    assert p["anthropic_version"] == "vertex-2023-10-16"      # Vertex: version in body
    assert "model" not in p                                   # model is named in the URL, not the body
    assert p["max_tokens"] == 1024                            # required; defaulted when caller passes 0
    assert cap["headers"]["Authorization"] == "Bearer k"     # Vertex GCP token, not x-api-key


def test_tools_declared_as_input_schema():
    cap = {}
    m = _model({"content": [{"type": "text", "text": "hi"}]}, capture=cap)
    run(m.converse(system="s", messages=[{"role": "user", "content": "hey"}], tools=TOOLS))
    t = cap["payload"]["tools"][0]
    assert t["name"] == "check_availability" and "input_schema" in t
    assert t["input_schema"]["required"] == ["day"]
    assert "type" not in t                                    # not the OpenAI {type:function} wrapper


def test_tool_use_block_is_parsed_as_a_tool_call():
    body = {"content": [{"type": "tool_use", "id": "toolu_1", "name": "check_availability",
                         "input": {"day": "tuesday"}}],
            "usage": {"input_tokens": 9, "output_tokens": 3}}
    out = run(_model(body).converse(system="s", messages=[{"role": "user", "content": "x"}],
                                    tools=TOOLS))
    assert out["kind"] == "tool_call"
    assert out["calls"][0] == {"id": "toolu_1", "name": "check_availability",
                               "arguments": {"day": "tuesday"}}
    assert out["tokens_in"] == 9 and out["tokens_out"] == 3


def test_a_tool_result_turn_becomes_a_user_tool_result_block():
    cap = {}
    m = _model({"content": [{"type": "text", "text": "done"}]}, capture=cap)
    transcript = [
        {"role": "user", "content": "when are you free?"},
        {"role": "assistant", "tool_calls": [{"id": "toolu_1", "name": "check_availability",
                                              "arguments": {"day": "tue"}}]},
        {"role": "tool", "tool_call_id": "toolu_1", "name": "check_availability",
         "content": "10:00, 11:00"},
    ]
    run(m.converse(system="s", messages=transcript, tools=TOOLS))
    msgs = cap["payload"]["messages"]
    # the assistant turn carries a tool_use block; the tool turn is a USER turn with tool_result
    assert any(b.get("type") == "tool_use" for b in msgs[1]["content"])
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_1",
                                     "content": "10:00, 11:00"}


def test_ephemeral_cache_marks_the_system_block():
    cap = {}
    m = _model({"content": [{"type": "text", "text": "hi"}]}, capture=cap, prompt_cache="ephemeral")
    run(m.complete(system="ground rules", user="hi"))
    sysblk = cap["payload"]["system"]
    assert sysblk == [{"type": "text", "text": "ground rules",
                       "cache_control": {"type": "ephemeral"}}]
