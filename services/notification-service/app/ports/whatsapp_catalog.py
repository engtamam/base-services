"""WhatsApp catalog management port — create/read a catalog for a WABA, connect it, set a phone
number's commerce settings.

This is the CATALOG lifecycle (the container of products), distinct from product SYNC (the items in
it, Phase 2) and from sending product messages (Phase 3). Content-agnostic: a consuming platform
decides what catalogs exist and how they map to its shops; this port only shapes the Graph calls.

A network/HTTP failure is a `CatalogResult(ok=False, error=...)`, never a crash — an adapter is
ordinary code and a Meta outage must not 500 the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class CatalogResult:
    ok: bool
    catalog_id: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "catalog_id": self.catalog_id, "error": self.error}


@dataclass
class ProductItem:
    """One product to upsert. `retailer_id` is the SHOP's own stable id for the product (Meta's
    `id`) — the idempotency key across syncs. Money is INTEGER minor units + currency (never a float);
    the adapter formats it to Meta's `"129.00 SAR"` shape."""
    retailer_id: str
    name: str
    price_minor: int
    currency: str = "SAR"
    availability: str = "in stock"       # in stock | out of stock | preorder
    condition: str = "new"               # new | used | refurbished
    description: str = ""
    image_url: str = ""
    url: str = ""
    brand: str = ""


@dataclass
class BatchResult:
    ok: bool
    handles: list[str] = field(default_factory=list)   # Meta's async batch handles, for a status check
    count: int = 0                                      # items submitted in this batch
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "handles": self.handles, "count": self.count, "error": self.error}


@runtime_checkable
class WhatsAppCatalogManager(Protocol):
    driver: str

    async def create_catalog(self, *, name: str) -> CatalogResult:
        """Create a product catalog owned by the (per-tenant) business. -> its catalog id."""
        ...

    async def get_catalog(self, *, catalog_id: str) -> CatalogResult:
        """Read a catalog's basic fields (name, product_count). ok=False if it does not exist."""
        ...

    async def list_catalogs(self) -> list[dict[str, Any]]:
        """The catalogs connected to this WABA — [{id, name}]. Empty on error (never raises)."""
        ...

    async def connect_catalog(self, *, catalog_id: str) -> CatalogResult:
        """Connect an owned catalog to this WABA so its products can be sent in chat."""
        ...

    async def set_commerce_settings(self, *, phone_number_id: str, is_cart_enabled: bool,
                                    is_catalog_visible: bool) -> CatalogResult:
        """Set a business phone number's WhatsApp commerce settings (cart + catalog visibility)."""
        ...

    async def sync_products(self, *, catalog_id: str, upserts: list[ProductItem],
                            deletes: list[str]) -> BatchResult:
        """Upsert and/or delete products in ONE batch (Meta's items_batch). Idempotent by
        retailer_id — a re-sync of the same items is safe. Returns the async batch handles."""
        ...
