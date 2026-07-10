"""HubSpot — verified: POST https://api.hubapi.com/oauth/v1/token, form-encoded,
`grant_type` + client_id + client_secret in the BODY, `expires_in` ≈ 1800s.

HubSpot's refresh token does NOT rotate (it lives until the app is uninstalled), so the
token endpoint may omit `refresh_token` on refresh — the caller must keep the existing one
rather than blanking it. That asymmetry is exactly why `Tokens.refresh_token` is allowed to
come back empty.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from app.adapters.connector_http import bearer, get_json, post_form, post_json
from app.core.mapping import get_path
from app.ports.connector_provider import CredentialInvalid, Page, Tokens

_TOKEN = "https://api.hubapi.com/oauth/v1/token"
_BASE = "https://api.hubapi.com"


class HubSpotConnector:
    driver = "hubspot"

    def _token_call(self, spec: dict, fields: dict) -> Tokens:
        code, body, _ = post_form(spec.get("token_url", _TOKEN), {
            **fields, "client_id": spec.get("client_id", ""),
            "client_secret": spec.get("client_secret", "")})
        if not (200 <= code < 300) or not body.get("access_token"):
            raise CredentialInvalid(f"hubspot oauth failed: {body.get('status', code)}")
        return Tokens(access_token=body["access_token"],
                      refresh_token=body.get("refresh_token", ""),   # absent on refresh: keep old
                      expires_in=int(body.get("expires_in", 1800) or 1800),
                      raw={"token_type": body.get("token_type", "")})

    def exchange(self, *, code: str, redirect_uri: str, spec: dict[str, Any]) -> Tokens:
        return self._token_call(spec, {"grant_type": "authorization_code", "code": code,
                                       "redirect_uri": redirect_uri})

    def refresh(self, *, refresh_token: str, spec: dict[str, Any]) -> Tokens:
        return self._token_call(spec, {"grant_type": "refresh_token", "refresh_token": refresh_token})

    def fetch(self, *, access_token, resource, cursor, limit, spec) -> Page:
        params = {"limit": limit}
        if cursor:
            params["after"] = cursor
        path = (spec.get("endpoints") or {}).get(resource, {}).get(
            "path", f"/crm/v3/objects/{resource}")
        url = f"{spec.get('base_url', _BASE).rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
        _c, body, _h = get_json(url, {"Authorization": bearer(access_token)})
        records = body.get("results") or []
        return Page(records=records if isinstance(records, list) else [],
                    next_cursor=str(get_path(body, "paging.next.after") or ""))

    def push(self, *, access_token, resource, record, idempotency_key, spec) -> dict[str, Any]:
        path = (spec.get("endpoints") or {}).get(resource, {}).get(
            "path", f"/crm/v3/objects/{resource}")
        url = f"{spec.get('base_url', _BASE).rstrip('/')}{path}"
        _c, body, _h = post_json(url, record, {"Authorization": bearer(access_token),
                                               "Idempotency-Key": idempotency_key})
        return body

    def revoke(self, *, access_token, spec) -> bool:
        code, _b, _h = post_json(f"{_BASE}/oauth/v1/refresh-tokens/{access_token}", {})
        return 200 <= code < 300
