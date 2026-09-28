"""Spec-driven generic OAuth2 + REST connector — most providers become CONFIG, not code.

A ProviderSpec describes the provider:

    {"token_url": "https://api.hubapi.com/oauth/v1/token",
     "base_url":  "https://api.hubapi.com",
     "client_id": "...", "client_secret": "...",
     "auth_style": "body" | "basic",          # where client credentials go
     "endpoints": {"contacts": {"path": "/crm/v3/objects/contacts",
                                "records_path": "results",
                                "cursor_param": "after",
                                "next_cursor_path": "paging.next.after",
                                "limit_param": "limit"}}}

Anything that speaks OAuth2 + JSON REST is now a config entry. Rotated refresh tokens are
returned as-is so the caller can persist them — a provider that rotates and a caller that
ignores the new token is the classic way to permanently brick an integration.
"""
from __future__ import annotations

from typing import Any

from app.adapters.connector_http import basic_auth, bearer, get_json, post_json
from app.core.mapping import get_path
from app.ports.connector_provider import CredentialInvalid, Page, ProviderUnavailable, Tokens


class OAuth2RestConnector:
    driver = "oauth2-rest"

    # ── auth ──────────────────────────────────────────────────────────────────
    def _token_call(self, spec: dict, fields: dict) -> Tokens:
        headers: dict[str, str] = {}
        if spec.get("auth_style") == "basic":
            headers["Authorization"] = basic_auth(spec.get("client_id", ""), spec.get("client_secret", ""))
        else:
            fields = {**fields, "client_id": spec.get("client_id", ""),
                      "client_secret": spec.get("client_secret", "")}
        from app.adapters.connector_http import post_form
        code, body, _ = post_form(spec["token_url"], fields, headers)
        if not (200 <= code < 300) or not body.get("access_token"):
            raise CredentialInvalid(f"token endpoint returned {code}")
        return Tokens(access_token=body["access_token"],
                      refresh_token=body.get("refresh_token", ""),   # may be ROTATED
                      expires_in=int(body.get("expires_in", 0) or 0),
                      scopes=str(body.get("scope", "")).split() if body.get("scope") else [],
                      raw={k: v for k, v in body.items() if "token" not in k})  # secret-free

    def exchange(self, *, code: str, redirect_uri: str, spec: dict[str, Any]) -> Tokens:
        return self._token_call(spec, {"grant_type": "authorization_code", "code": code,
                                       "redirect_uri": redirect_uri})

    def refresh(self, *, refresh_token: str, spec: dict[str, Any]) -> Tokens:
        return self._token_call(spec, {"grant_type": "refresh_token", "refresh_token": refresh_token})

    # ── data ──────────────────────────────────────────────────────────────────
    def _endpoint(self, spec: dict, resource: str) -> dict:
        ep = (spec.get("endpoints") or {}).get(resource)
        if not ep:
            raise CredentialInvalid(f"resource not in provider spec: {resource}")
        return ep

    def fetch(self, *, access_token, resource, cursor, limit, spec) -> Page:
        ep = self._endpoint(spec, resource)
        params = {ep.get("limit_param", "limit"): limit}
        if cursor:
            params[ep.get("cursor_param", "cursor")] = cursor
        import urllib.parse
        url = f"{spec['base_url'].rstrip('/')}{ep['path']}?{urllib.parse.urlencode(params)}"
        code, body, _h = get_json(url, {"Authorization": bearer(access_token)})
        # A provider 4xx (400/404/409 — the ones connector_http returns rather than raising) is NOT an
        # empty page (connector-002): treating it as records=[]/next='' marked the run 'succeeded' and
        # advanced/cleared the watermark, silently skipping data. Fail the run so backoff/circuit engage.
        if not (200 <= code < 300):
            raise ProviderUnavailable(f"provider returned {code} on fetch {resource}")
        records = get_path(body, ep.get("records_path", "results")) or []
        if not isinstance(records, list):
            records = []
        next_cursor = get_path(body, ep.get("next_cursor_path", "")) if ep.get("next_cursor_path") else None
        return Page(records=records, next_cursor=str(next_cursor or ""))

    def push(self, *, access_token, resource, record, idempotency_key, spec) -> dict[str, Any]:
        ep = self._endpoint(spec, resource)
        url = f"{spec['base_url'].rstrip('/')}{ep['path']}"
        headers = {"Authorization": bearer(access_token), "Idempotency-Key": idempotency_key}
        code, body, _h = post_json(url, record, headers)
        # A provider 4xx (validation/404/conflict) is a REJECTED write, not a success (connector-001):
        # returning the body regardless recorded records_out as written when nothing was. Raise so the
        # record dead-letters / the run fails instead of a phantom success.
        if not (200 <= code < 300):
            raise ProviderUnavailable(f"provider returned {code} on push {resource}")
        return body

    def revoke(self, *, access_token, spec) -> bool:
        url = spec.get("revoke_url")
        if not url:
            return True                       # nothing to call; the local disconnect stands
        code, _b, _h = post_json(url, {"token": access_token},
                                 {"Authorization": bearer(access_token)})
        return 200 <= code < 300
