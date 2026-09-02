"""notification-service over HTTP — POST /internal/notifications/send.

stdlib urllib, matching the other internal clients: no extra runtime dependency, a leaner image. The
URL comes from CONFIG only, never a request, so no caller can point a notification at a host of their
choosing. The body carries a `source` idempotency key notification-service de-duplicates on, so a
retry after an ambiguous failure notifies once, not twice.
"""
from __future__ import annotations

import json
from app.core.internal_client import assertion_headers_from_env
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.ports.notification_sender import NotificationRefused, NotificationUnavailable


class HttpNotificationSender:
    driver = "http"

    def __init__(self, url: str, token: str, *, allowed_hosts: frozenset[str] = frozenset(),
                 timeout: int = 5, path: str = "/internal/notifications/send") -> None:
        if not url:
            raise ValueError("NOTIFICATION_SERVICE_URL is required for the http notify driver")
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if allowed_hosts and host not in allowed_hosts:
            # Refused at construction: a destination the operator did not allow is a boot-time
            # mistake, and finding it at boot is the difference between a failed deploy and an
            # internal token posted somewhere unintended.
            raise ValueError(
                f"NOTIFICATION_SERVICE_URL host {host!r} is not in EGRESS_ALLOWED_HOSTS")
        self._url = url.rstrip("/") + path
        self._token = token
        self._timeout = timeout

    async def send(self, *, tenant: str, template: str, event: str, ticket_id: str,
                   subject_ref: str, to: str, data: dict[str, Any], source: str) -> bool:
        payload = json.dumps({
            "tenant": tenant, "template": template, "event": event, "ticket_id": ticket_id,
            "subject_ref": subject_ref, "to": to, "data": data, "source": source,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Internal-Token"] = self._token
        headers.update(assertion_headers_from_env(target="notification-service"))
        req = urllib.request.Request(self._url, data=payload, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                status = resp.status
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404, 409, 422):
                # A bad template or unknown recipient will not fix itself on the thousandth attempt.
                raise NotificationRefused(f"notification refused (HTTP {exc.code})") from exc
            raise NotificationUnavailable(f"notification-service HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise NotificationUnavailable(f"notification-service unreachable: {exc}") from exc
        if 200 <= status < 300:
            return True
        raise NotificationUnavailable(f"notification-service HTTP {status}")

    async def health(self) -> bool:
        return True
