"""HTTP booking backend — delegates availability + reservation to the PLATFORM's own service.

One small contract, one config-only URL (never caller-supplied, so no SSRF surface): we POST a single
JSON body carrying an `op` and let the platform answer. The platform owns "who is free when" and the
actual reservation; we own only the WhatsApp Flow orchestration.

Every body also carries `customer` — WHO is booking, from the Flow's seed data ("" if unseeded) — so a
per-customer backend can scope options and attribute the reservation.

    POST {WHATSAPP_BOOKING_PROVIDER_URL}
      {"op":"options",  "tenant","customer","dimension","chosen"}          -> {"options":[{id,title,...}]}
      {"op":"calendar", "tenant","customer","chosen"}                      -> {min_date,max_date,unavailable_dates,include_days}
      {"op":"slots",    "tenant","customer","chosen","date"}               -> {"slots":[{id,title}]}
      {"op":"reserve",  "tenant","customer","selection","idempotency_key"} -> {"ok":bool,"ref"?,"reason"?}

`reserve` MUST be idempotent on `idempotency_key` and atomic on the slot at the platform's end — a
redelivered confirm and two racing customers must never double-book (the design's core invariant;
we cannot enforce it inside the platform's calendar, only require it).
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable

from app.core.booking_flow import ProviderUnavailable

# transport(url, payload, headers) -> (status, dict). Injected so tests are hermetic (no network).
Transport = Callable[[str, dict, dict], Awaitable[tuple[int, dict]]]


class HttpBookingBackend:
    driver = "http"

    def __init__(self, url: str, token: str = "", *, timeout: int = 8,
                 transport: Transport | None = None) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self._transport = transport or self._urllib_transport

    async def _call(self, op: str, **body: Any) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Internal-Token"] = self._token
        from app.core.internal_client import assertion_headers_from_env
        headers.update(assertion_headers_from_env(target="booking-provider"))
        status, data = await self._transport(self._url, {"op": op, **body}, headers)
        if not 200 <= status < 300 or not isinstance(data, dict):
            # A backend error must not look like "no availability" (empty) or a silent booking — the
            # engine surfaces the reason as a snackbar and keeps the customer on the same screen.
            raise ProviderUnavailable(f"booking provider returned HTTP {status}")
        return data

    async def options(self, *, tenant: str, dimension: str, chosen: dict[str, Any],
                      customer: str = "") -> list[dict]:
        out = await self._call("options", tenant=tenant, dimension=dimension, chosen=chosen,
                               customer=customer)
        return list(out.get("options", []))

    async def calendar(self, *, tenant: str, chosen: dict[str, Any],
                       customer: str = "") -> dict[str, Any]:
        return await self._call("calendar", tenant=tenant, chosen=chosen, customer=customer)

    async def slots(self, *, tenant: str, chosen: dict[str, Any], date: str,
                    customer: str = "") -> list[dict]:
        out = await self._call("slots", tenant=tenant, chosen=chosen, date=date, customer=customer)
        return list(out.get("slots", []))

    async def reserve(self, *, tenant: str, selection: dict[str, Any],
                      idempotency_key: str, customer: str = "") -> dict[str, Any]:
        return await self._call("reserve", tenant=tenant, selection=selection,
                                idempotency_key=idempotency_key, customer=customer)

    async def _urllib_transport(self, url: str, payload: dict, headers: dict) -> tuple[int, dict]:
        # Blocking urllib off the event loop (see the blocking-sync-in-async lesson).
        def _post() -> tuple[int, dict]:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers,
                                         method="POST")
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
