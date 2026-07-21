"""HTTP embeddings — any provider that returns vectors for a batch of strings.

The URL and model come from CONFIG, never from a request: an embedder is asked to process the
user's question, so a caller-supplied endpoint would be a way to exfiltrate every question asked.

FAILURES ARE NOT FATAL. `embed` raises and the index catches it, falling back to lexical scoring.
An assistant that stops answering because a similarity service is down has turned an optional
quality improvement into a hard dependency.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.ports.errors import ConfigError

Transport = Callable[[str, dict, dict], Awaitable[tuple[int, Any]]]


class HttpEmbeddings:
    driver = "http"
    enabled = True

    def __init__(self, url: str, token: str = "", *, model: str = "", dimensions: int = 0,
                 timeout: float = 10.0, transport: Transport | None = None) -> None:
        if not url:
            raise ConfigError("EMBEDDINGS_URL is required for the http embeddings driver")
        self._url = url
        self._token = token
        self._model = model
        self.dimensions = dimensions
        self._timeout = timeout
        self._transport = transport or self._httpx_transport

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload: dict[str, Any] = {"input": texts}
        if self._model:
            payload["model"] = self._model
        status, body = await self._transport(self._url, headers, payload)
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"embeddings endpoint returned HTTP {status}")
        # Accept the two common shapes rather than insisting on one: {"data":[{"embedding":[...]}]}
        # and {"embeddings":[[...]]}. Refusing a provider over a key name helps nobody.
        rows = body.get("data")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            vectors = [r.get("embedding") or [] for r in rows]
        else:
            vectors = body.get("embeddings") or []
        if len(vectors) != len(texts):
            # A short batch would silently misalign vectors with entries — every subsequent
            # similarity would compare the wrong pair, and the answers would look merely poor.
            raise RuntimeError(
                f"embeddings endpoint returned {len(vectors)} vectors for {len(texts)} inputs")
        return [[float(x) for x in v] for v in vectors]

    async def health(self) -> bool:
        try:
            return bool(await self.embed(["health"]))
        except Exception:   # noqa: BLE001
            return False

    async def close(self) -> None:
        return None

    async def _httpx_transport(self, url, headers, payload):   # pragma: no cover - needs a provider
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
        try:
            return resp.status_code, resp.json()
        except Exception:   # noqa: BLE001
            return resp.status_code, None
