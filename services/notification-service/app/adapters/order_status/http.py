"""HTTP order-status backend — reads live status from the platform's order system over one config-only
URL (op=status). Read-only. urllib off the event loop."""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable

from app.core.booking_flow import ProviderUnavailable

Transport = Callable[[str, dict, dict], Awaitable[tuple[int, dict]]]


class HttpOrderStatusBackend:
    driver = "http"

    def __init__(self, url: str, token: str = "", *, timeout: int = 8,
                 transport: Transport | None = None) -> None:
        self._url, self._token, self._timeout = url, token, timeout
        self._transport = transport or self._urllib_transport

    async def status(self, *, tenant: str, order: str) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Internal-Token"] = self._token
        from app.core.internal_client import assertion_headers_from_env
        headers.update(assertion_headers_from_env(target="order-status-provider"))
        status, data = await self._transport(self._url, {"op": "status", "tenant": tenant, "order": order}, headers)
        if not 200 <= status < 300 or not isinstance(data, dict):
            raise ProviderUnavailable(f"order-status provider returned HTTP {status}")
        return data

    async def _urllib_transport(self, url, payload, headers) -> tuple[int, dict]:
        def _post():
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                    raw = resp.read()
                    try:
                        return resp.status, json.loads(raw or b"{}")
                    except ValueError:
                        return resp.status, {}
            except urllib.error.HTTPError as exc:
                return exc.code, {}
            except (urllib.error.URLError, OSError) as exc:
                raise ProviderUnavailable(f"order-status provider unreachable: {exc}") from None
        return await asyncio.to_thread(_post)
