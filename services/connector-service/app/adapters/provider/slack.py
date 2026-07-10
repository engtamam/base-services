"""Slack — verified: POST https://slack.com/api/oauth.v2.access, form-encoded, client
credentials via HTTP Basic (Slack's own recommendation). Note Slack answers 200 with
`{"ok": false, "error": ...}` on failure — a status check alone is not enough.

Classic bot tokens do not expire (`expires_in` absent); with token rotation enabled Slack
returns `expires_in` + a rotated `refresh_token`, which we surface so the caller persists it.
"""
from __future__ import annotations

from typing import Any

from app.adapters.connector_http import basic_auth, bearer, get_json, post_form, post_json
from app.core.mapping import get_path
from app.ports.connector_provider import CredentialInvalid, Page, Tokens

_BASE = "https://slack.com/api"


class SlackConnector:
    driver = "slack"

    def _token_call(self, spec: dict, fields: dict) -> Tokens:
        code, body, _ = post_form(
            f"{spec.get('base_url', _BASE).rstrip('/')}/oauth.v2.access", fields,
            {"Authorization": basic_auth(spec.get("client_id", ""), spec.get("client_secret", ""))})
        if not (200 <= code < 300) or not body.get("ok"):
            raise CredentialInvalid(f"slack oauth failed: {body.get('error', code)}")
        access = body.get("access_token") or (body.get("authed_user") or {}).get("access_token", "")
        if not access:
            raise CredentialInvalid("slack returned no access token")
        return Tokens(access_token=access, refresh_token=body.get("refresh_token", ""),
                      expires_in=int(body.get("expires_in", 0) or 0),
                      scopes=str(body.get("scope", "")).split(","),
                      raw={"team": (body.get("team") or {}).get("id", "")})

    def exchange(self, *, code: str, redirect_uri: str, spec: dict[str, Any]) -> Tokens:
        return self._token_call(spec, {"code": code, "redirect_uri": redirect_uri})

    def refresh(self, *, refresh_token: str, spec: dict[str, Any]) -> Tokens:
        return self._token_call(spec, {"grant_type": "refresh_token", "refresh_token": refresh_token})

    def fetch(self, *, access_token, resource, cursor, limit, spec) -> Page:
        import urllib.parse
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        url = f"{spec.get('base_url', _BASE).rstrip('/')}/{resource}?{urllib.parse.urlencode(params)}"
        _c, body, _h = get_json(url, {"Authorization": bearer(access_token)})
        if not body.get("ok", False):
            raise CredentialInvalid(f"slack error: {body.get('error', 'unknown')}")
        records = get_path(body, spec.get("records_path", "channels")) or []
        return Page(records=records if isinstance(records, list) else [],
                    next_cursor=str(get_path(body, "response_metadata.next_cursor") or ""))

    def push(self, *, access_token, resource, record, idempotency_key, spec) -> dict[str, Any]:
        url = f"{spec.get('base_url', _BASE).rstrip('/')}/{resource}"
        _c, body, _h = post_json(url, record, {"Authorization": bearer(access_token)})
        if not body.get("ok", False):
            raise CredentialInvalid(f"slack error: {body.get('error', 'unknown')}")
        return body

    def revoke(self, *, access_token, spec) -> bool:
        _c, body, _h = post_json(f"{spec.get('base_url', _BASE).rstrip('/')}/auth.revoke", {},
                                 {"Authorization": bearer(access_token)})
        return bool(body.get("ok"))
