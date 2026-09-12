"""HTTP model provider — one adapter, several vendors, no SDK.

WHY NO VENDOR SDK. Each one adds a dependency, a licence to audit, a CVE feed to watch and a
release cadence to track — per provider, and an adopter who wants a different one pays it again.
The wire format is a JSON POST returning a string. A vendor SDK to build that is a large amount of
supply chain for a small amount of convenience, and it is the sort of dependency that quietly makes
an image three times its size.

TWO SHAPES COVER ALMOST EVERYTHING. The chat-completions shape (messages array, `choices[].message`)
and the generate shape (`contents[].parts[].text`, `candidates[]`). Both are configured rather than
guessed, because auto-detecting a response shape means an unexpected payload is silently read as
"no answer" — an abstention that looks like the KB is thin when the real problem is a schema
mismatch.

THE PROMPT SAYS WHAT THE SERVICE ALREADY ENFORCES, AND IS NOT TRUSTED TO. The instruction to answer
only from the passages is worth sending — a model given it does better — but the service verifies
citations afterwards regardless, because an instruction is a request and a check is a guarantee. A
provider swap must never change whether the assistant will guess.

FAILURES NEVER REACH THE CALLER VERBATIM. Provider errors carry hostnames, model names, quota
details and sometimes fragments of the prompt. Every failure here becomes a ModelError whose text
the API layer discards.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from app.ports.errors import ConfigError
from app.ports.model import ModelAnswer, ModelError, Passage

# transport(url, headers, payload, timeout) -> (status, json)
Transport = Callable[[str, dict, dict, float], Awaitable[tuple[int, Any]]]
# (tenant) -> the credential to authenticate this call with. Asked per call rather than captured at
# construction, which is what lets an operator rotate the key without a redeploy; the resolver
# owns the caching so this stays off the hot path. The tenant lets a resolver return a TENANT-scoped
# key (a tenant that configured its own MODEL_API_KEY) and fall back to the platform key — a resolver
# that ignores tenant (e.g. Vertex) simply accepts and drops it.
KeyResolver = Callable[..., Awaitable[str]]

# Request-scoped tenant for the shared model singleton. Set by the entry points (KB /ask, support
# triage/draft) so _credential can hand the tenant to the key resolver WITHOUT threading it through
# every answering/kb layer. contextvars are per-asyncio-task, so concurrent requests never bleed.
import contextvars as _contextvars  # noqa: E402
current_tenant: "_contextvars.ContextVar[str]" = _contextvars.ContextVar(
    "assistant_model_tenant", default="")

CHAT, GENERATE, ANTHROPIC = "chat", "generate", "anthropic"
# Anthropic requires max_tokens; when a caller passes 0 we send this so a request never 400s on it.
_ANTHROPIC_DEFAULT_MAX_TOKENS = 1024
# The Messages API version sent as a HEADER on the DIRECT Anthropic API (api.anthropic.com). On Vertex
# rawPredict it goes in the BODY instead (MODEL_ANTHROPIC_VERSION, e.g. "vertex-2023-10-16").
_ANTHROPIC_API_HEADER_VERSION = "2023-06-01"


def _as_text(content: Any) -> str:
    """A transcript content coerced to a string for a text field. A dict/list (a structured tool
    result) is JSON-encoded rather than str()'d, so the model sees valid JSON, not a Python repr."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def _loads_args(arguments: Any) -> dict:
    """OpenAI returns tool-call arguments as a JSON STRING. Parse to a dict; a malformed or empty
    string yields {} rather than raising — a caller validates the arguments against its own schema."""
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}

_SYSTEM = (
    "You answer questions about a product using ONLY the numbered passages provided. "
    "Cite every passage you use as [n]. If the passages do not contain the answer, say you do not "
    "know — do not use any other knowledge. Answer in the same language as the question."
)


class HttpModel:
    driver = "http"

    def __init__(self, url: str, api_key: str = "", *, model: str = "", shape: str = CHAT,
                 timeout: float = 20.0, temperature: float = 0.2,
                 transport: Transport | None = None,
                 key_resolver: KeyResolver | None = None,
                 prompt_cache: str = "off", gemini_cached_content: str = "",
                 anthropic_version: str = "", extra_headers: dict[str, str] | None = None) -> None:
        if not url:
            raise ConfigError("MODEL_URL is required for the http model driver")
        if shape not in (CHAT, GENERATE, ANTHROPIC):
            raise ConfigError(
                f"MODEL_SHAPE must be {CHAT!r}, {GENERATE!r} or {ANTHROPIC!r}, got {shape!r}")
        self._url = url
        self._key = api_key
        self._model = model
        self._shape = shape
        self._timeout = timeout
        self._temperature = temperature
        self._transport = transport or self._httpx_transport
        self._key_resolver = key_resolver
        # PROMPT CACHING (cost). Gemini + OpenAI cache the stable PREFIX implicitly (system block first,
        # variable question last — the ordering this adapter already uses), so those are free and need
        # nothing sent. `prompt_cache="ephemeral"` adds Anthropic's explicit cache_control marker on the
        # (stable) system block — the one provider that requires an opt-in flag for its 90% discount;
        # only set it for an Anthropic-style endpoint. `gemini_cached_content` passes an explicit Vertex/
        # Gemini CachedContent resource name through on the generate shape, for a pre-created cache.
        self._prompt_cache = prompt_cache
        self._gemini_cached_content = gemini_cached_content
        # ANTHROPIC shape only. Non-empty ⇒ Vertex Model Garden rawPredict (publishers/anthropic/...:
        # rawPredict): the version rides in the BODY and the model is named in the URL (not the body),
        # authenticated by the Bearer GCP token. Empty ⇒ the direct Anthropic API (model in the body,
        # x-api-key + anthropic-version headers). Verified vs Meta/Google + Anthropic docs 2026-09-09.
        self._anthropic_version = anthropic_version
        # Static per-call headers merged into every request (after auth) — e.g. the Vertex priority
        # tier. Empty by default, so the wire is byte-identical to before unless a caller opts in.
        self._extra_headers = dict(extra_headers or {})

    def _auth_headers(self, key: str) -> dict[str, str]:
        """The auth headers for THIS endpoint. Bearer everywhere (OpenAI/Gemini/Vertex — incl. a Vertex
        GCP token for Claude-on-Vertex), EXCEPT the direct Anthropic API, which uses x-api-key +
        anthropic-version instead."""
        if not key:
            return {}
        if self._shape == ANTHROPIC and not self._anthropic_version:
            return {"x-api-key": key, "anthropic-version": _ANTHROPIC_API_HEADER_VERSION}
        return {"Authorization": f"Bearer {key}"}

    def _anthropic_system(self, system: str) -> Any:
        """System as a cache-marked block when ephemeral caching is on, else a plain string."""
        if self._prompt_cache == "ephemeral":
            return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        return system

    def _anthropic_envelope(self, body: dict[str, Any], max_tokens: int) -> dict[str, Any]:
        """Fields common to every Anthropic request: max_tokens (required), and either the body-level
        anthropic_version (Vertex rawPredict, model in the URL) or the model (direct API)."""
        body["max_tokens"] = max_tokens or _ANTHROPIC_DEFAULT_MAX_TOKENS
        body["temperature"] = self._temperature
        if self._anthropic_version:
            body["anthropic_version"] = self._anthropic_version   # Vertex: model is in the URL
        elif self._model:
            body["model"] = self._model                            # direct API: model in the body
        return body

    def use_key_resolver(self, resolver: KeyResolver) -> None:
        """Take the credential from the operator store instead of the boot-time config value.

        Set from the composition root rather than the constructor so the driver registry keeps its
        single shape: an adapter built without a resolver behaves exactly as before, which is what
        every existing test asserts.
        """
        self._key_resolver = resolver

    async def _credential(self) -> str:
        """The key for THIS call. The configured value when nothing overrides it. The resolver is
        given the request-scoped tenant so a tenant that set its own key is served (and billed) on it,
        not silently on the platform key."""
        if self._key_resolver is None:
            return self._key
        return await self._key_resolver(current_tenant.get()) or ""

    # ── prompt ────────────────────────────────────────────────────────────────
    @staticmethod
    def _prompt(question: str, passages: tuple[Passage, ...], persona: str) -> tuple[str, str]:
        """(system, user). Passages are NUMBERED so a citation is checkable.

        Numbers rather than raw ids: a model asked to reproduce "billing.vat#0" will mistype it,
        and a mistyped ref fails the citation check and costs a correct answer. It maps [1] back to
        the ref itself.
        """
        lines = []
        for i, p in enumerate(passages, 1):
            lines.append(f"[{i}] {p.title}\n{p.text}")
        system = _SYSTEM + (f"\n\n{persona}" if persona else "")
        return system, "Passages:\n\n" + "\n\n".join(lines) + f"\n\nQuestion: {question}"

    def _payload(self, system: str, user: str, max_tokens: int) -> dict:
        if self._shape == ANTHROPIC:
            return self._anthropic_envelope(
                {"system": self._anthropic_system(system),
                 "messages": [{"role": "user", "content": user}]}, max_tokens)
        if self._shape == CHAT:
            # The stable system block is the cache prefix. For Anthropic, mark it explicitly so its
            # 90% cached-read discount applies; for OpenAI/OpenRouter the marker is unnecessary (they
            # cache automatically) and is only sent when the operator has opted in for an Anthropic
            # endpoint. A plain string otherwise, unchanged for every existing provider.
            if self._prompt_cache == "ephemeral":
                system_content: Any = [{"type": "text", "text": system,
                                        "cache_control": {"type": "ephemeral"}}]
            else:
                system_content = system
            body: dict[str, Any] = {
                "messages": [{"role": "system", "content": system_content},
                             {"role": "user", "content": user}],
                "temperature": self._temperature,
            }
            if self._model:
                body["model"] = self._model
            if max_tokens:
                body["max_tokens"] = max_tokens
            return body
        body = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {"temperature": self._temperature},
        }
        if max_tokens:
            body["generationConfig"]["maxOutputTokens"] = max_tokens
        # An explicit, pre-created Vertex/Gemini context cache (implicit caching still applies with
        # no field; this is only for a cache the operator created and pinned by resource name).
        if self._gemini_cached_content:
            body["cachedContent"] = self._gemini_cached_content
        return body

    @staticmethod
    def _extract(shape: str, body: dict) -> tuple[str, int, int]:
        """(text, tokens_in, tokens_out) from any response shape."""
        usage = body.get("usage") or body.get("usageMetadata") or {}
        tin = int(usage.get("prompt_tokens") or usage.get("promptTokenCount")
                  or usage.get("input_tokens") or 0)
        tout = int(usage.get("completion_tokens") or usage.get("candidatesTokenCount")
                   or usage.get("output_tokens") or 0)

        if shape == ANTHROPIC:
            for block in body.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    return str(block["text"]), tin, tout
            return "", tin, tout

        if shape == CHAT:
            choices = body.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                return str(msg.get("content") or ""), tin, tout
            return "", tin, tout

        for cand in body.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                if part.get("text"):
                    return str(part["text"]), tin, tout
        return "", tin, tout

    @staticmethod
    def _cited(text: str, passages: tuple[Passage, ...]) -> tuple[str, ...]:
        """Map the [n] markers back to refs. Only numbers that exist are returned.

        Anything else is dropped here and the service's own citation check drops it again — an
        out-of-range [9] is a model inventing a source, and that is the moment to stop trusting the
        sentence around it.
        """
        used = []
        for i, p in enumerate(passages, 1):
            if f"[{i}]" in text:
                used.append(p.ref)
        return tuple(used)

    # ── tool / function calling (for an AI connection that declares tools) ───────
    # A connection may carry tool declarations and run a multi-turn loop: the model either answers
    # in text or asks to CALL a tool, the caller executes it and re-invokes with the result appended.
    # Both wire shapes are supported and were verified against the providers' current first-party docs
    # (2026-09): OpenAI Chat Completions `tools`/`tool_calls` and Gemini generateContent
    # `functionDeclarations`/`functionCall`. The transcript this method takes is PROVIDER-NEUTRAL —
    # the caller never speaks a vendor's wire shape — and is translated here per shape.
    def _tools_payload(self, tools: list[dict]) -> Any:
        """Vendor tool-declaration shape. `tools` items are {name, description, parameters}."""
        if self._shape == ANTHROPIC:
            return [{"name": t["name"], "description": t.get("description", ""),
                     "input_schema": t.get("parameters") or {"type": "object", "properties": {}}}
                    for t in tools]
        if self._shape == CHAT:
            return [{"type": "function",
                     "function": {"name": t["name"], "description": t.get("description", ""),
                                  "parameters": t.get("parameters") or {"type": "object",
                                                                        "properties": {}}}}
                    for t in tools]
        # generate: a single tool object whose functionDeclarations lists every function.
        return [{"functionDeclarations": [
            {"name": t["name"], "description": t.get("description", ""),
             "parameters": t.get("parameters") or {"type": "object", "properties": {}}}
            for t in tools]}]

    def _messages_payload(self, system: str, messages: list[dict], tools: list[dict],
                          max_tokens: int) -> dict:
        """Translate the neutral transcript into the vendor request body.

        Neutral turns: {"role":"user"|"assistant","content":str},
        {"role":"assistant","tool_calls":[{"id","name","arguments":dict}]},
        {"role":"tool","tool_call_id":str,"name":str,"content":str}.
        """
        if self._shape == ANTHROPIC:
            msgs: list[dict] = []
            for m in messages:
                role = m.get("role")
                if role == "assistant" and m.get("tool_calls"):
                    blocks: list[dict] = []
                    if m.get("content"):
                        blocks.append({"type": "text", "text": _as_text(m.get("content"))})
                    for i, c in enumerate(m["tool_calls"]):
                        blocks.append({"type": "tool_use", "id": c.get("id") or f"call_{i}",
                                       "name": c["name"], "input": c.get("arguments") or {}})
                    msgs.append({"role": "assistant", "content": blocks})
                elif role == "tool":
                    # a tool result is a USER turn carrying a tool_result block in the Messages API.
                    msgs.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                         "content": _as_text(m.get("content"))}]})
                else:
                    msgs.append({"role": "assistant" if role == "assistant" else "user",
                                 "content": _as_text(m.get("content"))})
            body = self._anthropic_envelope(
                {"system": self._anthropic_system(system), "messages": msgs}, max_tokens)
            if tools:
                body["tools"] = self._tools_payload(tools)
            return body
        if self._shape == CHAT:
            if self._prompt_cache == "ephemeral":
                sys_content: Any = [{"type": "text", "text": system,
                                     "cache_control": {"type": "ephemeral"}}]
            else:
                sys_content = system
            out: list[dict] = [{"role": "system", "content": sys_content}]
            for m in messages:
                role = m.get("role")
                if role == "assistant" and m.get("tool_calls"):
                    out.append({"role": "assistant", "content": m.get("content"),
                                "tool_calls": [
                                    {"id": c.get("id") or f"call_{i}", "type": "function",
                                     "function": {"name": c["name"],
                                                  "arguments": json.dumps(c.get("arguments") or {})}}
                                    for i, c in enumerate(m["tool_calls"])]})
                elif role == "tool":
                    out.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""),
                                "content": _as_text(m.get("content"))})
                else:
                    out.append({"role": role or "user", "content": _as_text(m.get("content"))})
            body: dict[str, Any] = {"messages": out, "temperature": self._temperature}
            if self._model:
                body["model"] = self._model
            if max_tokens:
                body["max_tokens"] = max_tokens
            if tools:
                body["tools"] = self._tools_payload(tools)
            return body
        # generate shape
        contents: list[dict] = []
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                contents.append({"role": "model", "parts": [
                    {"functionCall": {"name": c["name"], "args": c.get("arguments") or {}}}
                    for c in m["tool_calls"]]})
            elif role == "tool":
                resp = m.get("content")
                resp = resp if isinstance(resp, dict) else {"result": _as_text(resp)}
                part: dict[str, Any] = {"functionResponse": {"name": m.get("name", ""),
                                                             "response": resp}}
                if m.get("tool_call_id"):
                    part["functionResponse"]["id"] = m["tool_call_id"]
                contents.append({"role": "user", "parts": [part]})
            else:
                g_role = "model" if role == "assistant" else "user"
                contents.append({"role": g_role, "parts": [{"text": _as_text(m.get("content"))}]})
        body = {"contents": contents,
                "systemInstruction": {"parts": [{"text": system}]},
                "generationConfig": {"temperature": self._temperature}}
        if max_tokens:
            body["generationConfig"]["maxOutputTokens"] = max_tokens
        if tools:
            body["tools"] = self._tools_payload(tools)
        if self._gemini_cached_content:
            body["cachedContent"] = self._gemini_cached_content
        return body

    @staticmethod
    def _extract_converse(shape: str, body: dict) -> dict:
        """Discriminated result: {"kind":"text","text",...} or
        {"kind":"tool_call","calls":[{id,name,arguments}], ...}, plus token counts."""
        usage = body.get("usage") or body.get("usageMetadata") or {}
        tin = int(usage.get("prompt_tokens") or usage.get("promptTokenCount")
                  or usage.get("input_tokens") or 0)
        tout = int(usage.get("completion_tokens") or usage.get("candidatesTokenCount")
                   or usage.get("output_tokens") or 0)

        if shape == ANTHROPIC:
            calls, text = [], ""
            for block in body.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    calls.append({"id": block.get("id", ""), "name": block.get("name", ""),
                                  "arguments": block.get("input") or {}})
                elif block.get("type") == "text" and not text:
                    text = str(block.get("text") or "")
            if calls:
                return {"kind": "tool_call", "calls": calls, "tokens_in": tin, "tokens_out": tout}
            return {"kind": "text", "text": text, "tokens_in": tin, "tokens_out": tout}

        if shape == CHAT:
            choices = body.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
            calls = msg.get("tool_calls") or []
            if calls:
                parsed = []
                for c in calls:
                    fn = c.get("function") or {}
                    parsed.append({"id": c.get("id", ""), "name": fn.get("name", ""),
                                   "arguments": _loads_args(fn.get("arguments"))})
                return {"kind": "tool_call", "calls": parsed, "tokens_in": tin, "tokens_out": tout}
            return {"kind": "text", "text": str(msg.get("content") or ""),
                    "tokens_in": tin, "tokens_out": tout}

        parsed = []
        text = ""
        for cand in body.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                fc = part.get("functionCall")
                if fc:
                    parsed.append({"id": fc.get("id", ""), "name": fc.get("name", ""),
                                   "arguments": fc.get("args") or {}})
                elif part.get("text") and not text:
                    text = str(part["text"])
        if parsed:
            return {"kind": "tool_call", "calls": parsed, "tokens_in": tin, "tokens_out": tout}
        return {"kind": "text", "text": text, "tokens_in": tin, "tokens_out": tout}

    async def converse(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                       max_tokens: int = 0) -> dict:
        """ONE round trip of a (possibly tool-calling) conversation. Returns a discriminated result
        (text answer or tool-call request) plus token counts. The caller runs the loop: on a
        tool_call it executes the tool and calls again with the result appended to `messages`."""
        headers = {"Content-Type": "application/json"}
        key = await self._credential()
        headers.update(self._auth_headers(key))
        headers.update(self._extra_headers)      # e.g. Vertex priority tier; empty unless opted in
        payload = self._messages_payload(system, messages, tools or [], max_tokens)
        try:
            status, body = await self._transport(self._url, headers, payload, self._timeout)
        except Exception as exc:   # noqa: BLE001
            raise ModelError(f"model transport failed: {exc}") from exc
        if status != 200 or not isinstance(body, dict):
            raise ModelError(f"model endpoint returned HTTP {status}")
        result = self._extract_converse(self._shape, body)
        if result["kind"] == "text" and not result["text"].strip():
            raise ModelError("model returned no text")
        return result

    # ── raw completion (for an isolated AI connection with its OWN prompt) ────────
    async def complete(self, *, system: str, user: str, max_tokens: int = 0) -> tuple[str, int, int]:
        """A raw (system, user) completion — no KB framing, no citation rules. Returns
        (text, tokens_in, tokens_out). Used by the AI-connections feature, where each connection
        carries its own system prompt; the KB assistant path stays answer() with its grounding."""
        headers = {"Content-Type": "application/json"}
        key = await self._credential()
        headers.update(self._auth_headers(key))
        headers.update(self._extra_headers)      # e.g. Vertex priority tier; empty unless opted in
        try:
            status, body = await self._transport(self._url, headers,
                                                 self._payload(system, user, max_tokens),
                                                 self._timeout)
        except Exception as exc:   # noqa: BLE001
            raise ModelError(f"model transport failed: {exc}") from exc
        if status != 200 or not isinstance(body, dict):
            raise ModelError(f"model endpoint returned HTTP {status}")
        text, tin, tout = self._extract(self._shape, body)
        if not text.strip():
            raise ModelError("model returned no text")
        return text, tin, tout

    # ── the call ──────────────────────────────────────────────────────────────
    async def answer(self, *, question: str, passages: tuple[Passage, ...], lang: str,
                     persona: str = "", max_tokens: int = 0) -> ModelAnswer:
        system, user = self._prompt(question, passages, persona)
        headers = {"Content-Type": "application/json"}
        key = await self._credential()
        headers.update(self._auth_headers(key))
        headers.update(self._extra_headers)      # e.g. Vertex priority tier; empty unless opted in

        try:
            status, body = await self._transport(self._url, headers,
                                                 self._payload(system, user, max_tokens),
                                                 self._timeout)
        except Exception as exc:   # noqa: BLE001 — timeouts, DNS, TLS: all one condition here
            raise ModelError(f"model transport failed: {exc}") from exc

        if status != 200 or not isinstance(body, dict):
            # The body is deliberately NOT included: it carries hostnames, model names and quota
            # details, and the API layer would have to strip what we chose to attach.
            raise ModelError(f"model endpoint returned HTTP {status}")

        text, tin, tout = self._extract(self._shape, body)
        if not text.strip():
            # A 200 with no text is a failure, not an empty answer. Returning it as an answer would
            # surface as a blank reply that the honesty gate then reports as unsupported — true,
            # but it hides that the provider misbehaved.
            raise ModelError("model returned no text")

        return ModelAnswer(text=text, used_refs=self._cited(text, passages),
                           tokens_in=tin, tokens_out=tout)

    async def health(self) -> bool:
        return bool(self._url)

    async def close(self) -> None:
        return None

    async def _httpx_transport(self, url, headers, payload, timeout):   # pragma: no cover - live provider
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
        try:
            return resp.status_code, resp.json()
        except (json.JSONDecodeError, ValueError):
            return resp.status_code, None
