"""HTTP reschedule backend — delegates to the platform's own bookings service over the SAME config
URL as booking (op-based). Idempotency + atomicity on cancel/reschedule are the platform's, keyed on
the flow_token we pass. urllib off the event loop (blocking-sync-in-async lesson)."""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable

from app.core.booking_flow import ProviderUnavailable

Transport = Callable[[str, dict, dict], Awaitable[tuple[int, dict]]]


class HttpReschedulingBackend:
    driver = "http"

    def __init__(self, url: str, token: str = "", *, timeout: int = 8,
                 transport: Transport | None = None) -> None:
        self._url, self._token, self._timeout = url, token, timeout
        self._transport = transport or self._urllib_transport

    async def _call(self, op: str, **body: Any) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Internal-Token"] = self._token
        from app.core.internal_client import assertion_headers_from_env
        headers.update(assertion_headers_from_env(target="booking-provider"))
        status, data = await self._transport(self._url, {"op": op, **body}, headers)
        if not 200 <= status < 300 or not isinstance(data, dict):
            raise ProviderUnavailable(f"booking provider returned HTTP {status}")
        return data

    async def appointments(self, *, tenant, customer) -> list[dict]:
        return list((await self._call("appointments", tenant=tenant, customer=customer)).get("appointments", []))

    async def calendar(self, *, tenant, appointment) -> dict:
        return await self._call("calendar_for", tenant=tenant, appointment=appointment)

    async def slots(self, *, tenant, appointment, date) -> list[dict]:
        return list((await self._call("slots_for", tenant=tenant, appointment=appointment, date=date)).get("slots", []))

    async def cancel(self, *, tenant, appointment, idempotency_key) -> dict:
        return await self._call("cancel", tenant=tenant, appointment=appointment, idempotency_key=idempotency_key)

    async def reschedule(self, *, tenant, appointment, slot, idempotency_key) -> dict:
        return await self._call("reschedule", tenant=tenant, appointment=appointment, slot=slot,
                                idempotency_key=idempotency_key)

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
                raise ProviderUnavailable(f"booking provider unreachable: {exc}") from None
        return await asyncio.to_thread(_post)
