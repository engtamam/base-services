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

CHAT, GENERATE = "chat", "generate"

_SYSTEM = (
    "You answer questions about a product using ONLY the numbered passages provided. "
    "Cite every passage you use as [n]. If the passages do not contain the answer, say you do not "
    "know — do not use any other knowledge. Answer in the same language as the question."
)


class HttpModel:
    driver = "http"

    def __init__(self, url: str, api_key: str = "", *, model: str = "", shape: str = CHAT,
                 timeout: float = 20.0, temperature: float = 0.2,
                 transport: Transport | None = None) -> None:
        if not url:
            raise ConfigError("MODEL_URL is required for the http model driver")
        if shape not in (CHAT, GENERATE):
            raise ConfigError(f"MODEL_SHAPE must be {CHAT!r} or {GENERATE!r}, got {shape!r}")
        self._url = url
        self._key = api_key
        self._model = model
        self._shape = shape
        self._timeout = timeout
        self._temperature = temperature
        self._transport = transport or self._httpx_transport

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
        if self._shape == CHAT:
            body: dict[str, Any] = {
                "messages": [{"role": "system", "content": system},
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
        return body

    @staticmethod
    def _extract(shape: str, body: dict) -> tuple[str, int, int]:
        """(text, tokens_in, tokens_out) from either response shape."""
        usage = body.get("usage") or body.get("usageMetadata") or {}
        tin = int(usage.get("prompt_tokens") or usage.get("promptTokenCount") or 0)
        tout = int(usage.get("completion_tokens") or usage.get("candidatesTokenCount") or 0)

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

    # ── the call ──────────────────────────────────────────────────────────────
    async def answer(self, *, question: str, passages: tuple[Passage, ...], lang: str,
                     persona: str = "", max_tokens: int = 0) -> ModelAnswer:
        system, user = self._prompt(question, passages, persona)
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"

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
