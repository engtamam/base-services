"""WhatsApp inbound source — reads notification-service's durable inbound feed and normalises it.

GET {NOTIFICATION_SERVICE_URL}{path}?after=<cursor>&limit=<n>  ->  {"messages": [...], "cursor": "..."}
Each row carries wamid, from, tenant, timestamp and the raw Meta `message`. We map that to an
InboundMessage; the bridge is channel-agnostic and never sees this shape. URL is config-only (never a
request), host-allowlisted at construction — not an SSRF surface.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.core.inbound_bridge import InboundMessage
from app.core.internal_client import assertion_headers_from_env
from app.ports.inbound_source import InboundSourceUnavailable

_CHANNEL = "whatsapp"


def _text_of(message: dict[str, Any]) -> str:
    """Human body from a Meta message: the text body, else a caption, else a bare type marker so a
    non-text message (image/location/…) still opens a ticket with something legible."""
    mtype = str(message.get("type", "") or "")
    if mtype == "text":
        return str((message.get("text") or {}).get("body", "") or "")
    blk = message.get(mtype) or {}
    if isinstance(blk, dict) and blk.get("caption"):
        return str(blk["caption"])
    return f"[{mtype or 'message'}]"


class HttpWhatsAppInboundSource:
    driver = "http"

    def __init__(self, url: str, token: str, *, allowed_hosts: frozenset[str] = frozenset(),
                 timeout: int = 5, path: str = "/internal/whatsapp/inbound",
                 channel: str = _CHANNEL) -> None:
        if not url:
            raise ValueError("NOTIFICATION_SERVICE_URL is required for the http inbound source")
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if allowed_hosts and host not in allowed_hosts:
            raise ValueError(f"NOTIFICATION_SERVICE_URL host {host!r} is not in EGRESS_ALLOWED_HOSTS")
        self._base = url.rstrip("/") + path
        self._token = token
        self._timeout = timeout
        self._channel = channel

    async def read(self, *, after: str, limit: int) -> tuple[list[InboundMessage], str | None]:
        import asyncio
        q = urllib.parse.urlencode({"after": after or "", "limit": max(1, min(1000, limit))})
        headers = {"Accept": "application/json"}
        if self._token:
            headers["X-Internal-Token"] = self._token
        headers.update(assertion_headers_from_env(target="notification-service"))

        def _get() -> tuple[int, dict]:
            req = urllib.request.Request(f"{self._base}?{q}", method="GET", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                    return resp.status, json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                return exc.code, {}
            except (urllib.error.URLError, OSError) as exc:
                raise InboundSourceUnavailable(f"inbound feed unreachable: {exc}") from None

        status, data = await asyncio.to_thread(_get)
        if not 200 <= status < 300 or not isinstance(data, dict):
            raise InboundSourceUnavailable(f"inbound feed HTTP {status}")
        out: list[InboundMessage] = []
        for row in data.get("messages", []) or []:
            msg = row.get("message") or {}
            out.append(InboundMessage(
                channel=self._channel, tenant=str(row.get("tenant", "") or ""),
                sender=str(row.get("from", "") or ""), external_id=str(row.get("wamid", "") or ""),
                text=_text_of(msg), timestamp=float(row.get("timestamp", 0) or 0),
                meta={"type": str(msg.get("type", "") or "")}))
        return out, data.get("cursor")
