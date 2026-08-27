"""HTTP notifier — hand the message to notification-service.

    POST {NOTIFY_SERVICE_URL}/internal/notify
        {"recipient": …, "channel": "email", "template_key": …, "tenant": …,
         "variables": {…}, "idempotency_key": …}

THE IDEMPOTENCY KEY IS NOT OPTIONAL HERE. A verification email is triggered by an unauthenticated
route; a retried request, a double-clicked button or a client-side retry would otherwise send the
same person several mails carrying DIFFERENT live tokens — more live credentials for one address,
each another chance to leak. notification-service dedupes on this key, so a retry costs nothing.

The URL comes from CONFIG only, never from a request, so no caller can steer a message — and the
token it carries — at a host of their choosing.

Every failure path returns False rather than raising. The account exists; the human can ask for
another mail. Turning a provider hiccup into a failed registration would be the worse trade.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HttpNotifier:
    driver = "http"

    def __init__(self, url: str, token: str, timeout: int = 5,
                 path: str = "/internal/notify") -> None:
        self._url = url.rstrip("/") + path
        self._token = token
        self._timeout = timeout

    async def send(self, *, to: str, template: str, tenant: str,
                   variables: dict[str, Any] | None = None,
                   channel: str = "email") -> bool:
        payload = {
            "recipient": to,
            # The REAL channel notification-service routes on — was hardcoded "email", which quietly
            # made OTP_DELIVERY_CHANNEL do nothing. Defaults to email so the verification path is
            # unchanged; the OTP path passes whatsapp/sms through.
            "channel": channel or "email",
            "template_key": template,
            "tenant": tenant,
            "variables": dict(variables or {}),
            # Keyed on the TOKEN, not on the address or a timestamp: one token is one intended
            # message. Keying on the address would suppress a legitimate resend; keying on time
            # would defeat the dedupe entirely.
            "idempotency_key": f"verify:{(variables or {}).get('token_ref', '')}",
        }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self._url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        if self._token:
            req.add_header("X-Internal-Token", self._token)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                return 200 <= resp.status < 300
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            return False
