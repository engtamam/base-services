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

from decimal import Decimal

from app.adapters.notify_http import bearer, get_json, ok, post_json
from app.ports.errors import ConfigError
from app.ports.whatsapp_catalog import BatchResult, CatalogResult, ProductItem

_DEFAULT_BASE = "https://graph.facebook.com/v26.0"

# Currency minor-unit exponents (ISO 4217). Most are 2 dp; the Gulf 3-dp currencies and the 0-dp ones
# matter because a wrong exponent prices a product 10x/100x off. Default 2 for anything unlisted.
_EXPONENT = {"BHD": 3, "KWD": 3, "OMR": 3, "IQD": 3, "JOD": 3, "TND": 3,
             "JPY": 0, "KRW": 0}


def _format_price(amount_minor: int, currency: str) -> str:
    """Integer minor units -> Meta's `"129.00 SAR"` string (major amount + ISO code, never a float)."""
    cur = (currency or "SAR").upper()
    exp = _EXPONENT.get(cur, 2)
    major = (Decimal(int(amount_minor)) / (Decimal(10) ** exp)).quantize(Decimal(1).scaleb(-exp))
    return f"{major} {cur}"


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

    def _product_data(self, p: ProductItem) -> dict:
        data: dict = {
            "id": p.retailer_id,
            "title": p.name,
            "availability": p.availability,
            "condition": p.condition,
            "price": _format_price(p.price_minor, p.currency),
            "currency": (p.currency or "SAR").upper(),
        }
        if p.description:
            data["description"] = p.description
        if p.image_url:
            data["image_link"] = p.image_url
        if p.url:
            data["link"] = p.url
        if p.brand:
            data["brand"] = p.brand
        return data

    async def sync_products(self, *, catalog_id: str, upserts: list[ProductItem],
                            deletes: list[str]) -> BatchResult:
        # UPDATE upserts by retailer_id (Meta creates it if absent), so a re-sync is idempotent; DELETE
        # needs only the id. One items_batch call carries both, in a single round trip.
        requests: list[dict] = [
            {"method": "UPDATE", "data": self._product_data(p)} for p in upserts
        ] + [
            {"method": "DELETE", "data": {"id": rid}} for rid in deletes if rid
        ]
        if not requests:
            return BatchResult(ok=True, handles=[], count=0)
        status, body = post_json(
            f"{self._base}/{catalog_id}/items_batch",
            {"item_type": "PRODUCT_ITEM", "requests": requests}, self._headers)
        if not ok(status):
            return BatchResult(ok=False, error=self._err(status, body), count=len(requests))
        try:
            handles = _json.loads(body or "{}").get("handles") or []
        except (ValueError, TypeError):
            handles = []
        return BatchResult(ok=True, handles=[str(h) for h in handles], count=len(requests))
