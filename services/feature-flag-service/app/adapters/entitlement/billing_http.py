"""Billing entitlement checker — asks billing-service whether the plan allows `key`.

Fail-CLOSED: an unreachable/unconfigured billing-service denies the entitlement rather
than silently unlocking a paid feature.
"""
from __future__ import annotations

import urllib.parse

from app.adapters.flag_http import get_json
from app.core.internal_client import assertion_headers_from_env


class BillingHttpEntitlement:
    driver = "billing-http"

    def __init__(self, base_url: str, token: str) -> None:
        self._base = (base_url or "").rstrip("/")
        self._token = token

    def allowed(self, *, tenant: str, key: str) -> bool:
        if not (self._base and self._token):
            return False                                  # fail-closed
        q = urllib.parse.urlencode({"tenant": tenant, "key": key})
        code, resp = get_json(f"{self._base}/internal/billing/entitlements/check?{q}",
                              {"X-Internal-Token": self._token,
                               **assertion_headers_from_env(target="billing-service")})
        if not (200 <= code < 300):
            return False                                  # fail-closed
        return bool(resp.get("allowed", False))
