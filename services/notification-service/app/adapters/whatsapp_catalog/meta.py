"""Meta catalog management — Graph calls for a WABA's product catalog lifecycle.

Verified against Meta's Commerce/WhatsApp catalog docs (2026-09-16 — re-verify before relying):
  create  → POST /{business_id}/owned_product_catalogs         {name}            -> {id}
  read    → GET  /{catalog_id}?fields=name,product_count
  list    → GET  /{waba_id}/product_catalogs                                      -> {data:[{id,name}]}
  connect → POST /{waba_id}/product_catalogs                   {catalog_id}       (community-documented
            shape; some setups connect via Commerce Manager — this is the API path, isolated so it is
            trivially corrected if Meta's edge differs)
  settings→ POST /{phone_number_id}/whatsapp_commerce_settings {is_cart_enabled, is_catalog_visible}

Token, business id, and WABA id are per-tenant (resolved in core/provider_pool, never in a route);
the Graph base is config, never a request, so no caller can point these at a host of its choosing.
Every failure is a CatalogResult(ok=False, ...), never an exception.
"""
from __future__ import annotations

import json as _json

from app.adapters.notify_http import bearer, get_json, ok, post_json
from app.ports.errors import ConfigError
from app.ports.whatsapp_catalog import CatalogResult

_DEFAULT_BASE = "https://graph.facebook.com/v26.0"


class MetaCatalogManager:
    driver = "meta"

    def __init__(self, access_token: str, business_id: str, waba_id: str, base_url: str = "") -> None:
        if not access_token:
            raise ConfigError("WHATSAPP_ACCESS_TOKEN is required for the meta catalog manager")
        self._base = (base_url or _DEFAULT_BASE).rstrip("/")
        self._token = access_token
        self._business_id = business_id
        self._waba_id = waba_id
        self._headers = bearer(access_token)

    @staticmethod
    def _err(status: int, body: str) -> str:
        try:
            msg = (_json.loads(body or "{}").get("error") or {}).get("message", "")
        except (ValueError, TypeError):
            msg = ""
        return msg or f"HTTP {status}"

    async def create_catalog(self, *, name: str) -> CatalogResult:
        if not self._business_id:
            return CatalogResult(ok=False, error="no business id configured for this tenant")
        status, body = post_json(f"{self._base}/{self._business_id}/owned_product_catalogs",
                                 {"name": name}, self._headers)
        if not ok(status):
            return CatalogResult(ok=False, error=self._err(status, body))
        try:
            cid = str(_json.loads(body).get("id", "") or "")
        except (ValueError, TypeError):
            cid = ""
        return CatalogResult(ok=bool(cid), catalog_id=cid,
                             error="" if cid else "created but no id returned")

    async def get_catalog(self, *, catalog_id: str) -> CatalogResult:
        status, body = get_json(f"{self._base}/{catalog_id}", {"fields": "name,product_count"},
                                self._headers)
        if not ok(status):
            return CatalogResult(ok=False, error=self._err(status, body))
        try:
            data = _json.loads(body or "{}")
        except (ValueError, TypeError):
            data = {}
        return CatalogResult(ok=True, catalog_id=str(data.get("id", catalog_id)), raw=data)

    async def list_catalogs(self) -> list[dict]:
        if not self._waba_id:
            return []
        status, body = get_json(f"{self._base}/{self._waba_id}/product_catalogs", None, self._headers)
        if not ok(status):
            return []
        try:
            rows = _json.loads(body or "{}").get("data") or []
        except (ValueError, TypeError):
            return []
        return [{"id": str(r.get("id", "")), "name": str(r.get("name", ""))} for r in rows if r.get("id")]

    async def connect_catalog(self, *, catalog_id: str) -> CatalogResult:
        if not self._waba_id:
            return CatalogResult(ok=False, error="no WABA id configured for this tenant")
        status, body = post_json(f"{self._base}/{self._waba_id}/product_catalogs",
                                 {"catalog_id": catalog_id}, self._headers)
        if not ok(status):
            return CatalogResult(ok=False, catalog_id=catalog_id, error=self._err(status, body))
        return CatalogResult(ok=True, catalog_id=catalog_id)

    async def set_commerce_settings(self, *, phone_number_id: str, is_cart_enabled: bool,
                                    is_catalog_visible: bool) -> CatalogResult:
        if not phone_number_id:
            return CatalogResult(ok=False, error="phone_number_id is required")
        status, body = post_json(
            f"{self._base}/{phone_number_id}/whatsapp_commerce_settings",
            {"is_cart_enabled": bool(is_cart_enabled), "is_catalog_visible": bool(is_catalog_visible)},
            self._headers)
        if not ok(status):
            return CatalogResult(ok=False, error=self._err(status, body))
        return CatalogResult(ok=True)
