"""Console catalog manager — records the call to the log and returns synthetic ids. Zero infra
(the dev/default driver), so the whole catalog lifecycle runs without a real WABA."""
from __future__ import annotations

import uuid

from app.observability.logging import get_logger
from app.ports.whatsapp_catalog import CatalogResult

_log = get_logger("app.whatsapp_catalog.console")


class ConsoleCatalogManager:
    driver = "console"

    async def create_catalog(self, *, name: str) -> CatalogResult:
        cid = f"catalog:{uuid.uuid4().hex}"
        _log.info("catalog_created", extra={"provider": "console", "name": name, "catalog_id": cid})
        return CatalogResult(ok=True, catalog_id=cid)

    async def get_catalog(self, *, catalog_id: str) -> CatalogResult:
        return CatalogResult(ok=True, catalog_id=catalog_id,
                             raw={"id": catalog_id, "name": "console", "product_count": 0})

    async def list_catalogs(self) -> list[dict]:
        return []

    async def connect_catalog(self, *, catalog_id: str) -> CatalogResult:
        _log.info("catalog_connected", extra={"provider": "console", "catalog_id": catalog_id})
        return CatalogResult(ok=True, catalog_id=catalog_id)

    async def set_commerce_settings(self, *, phone_number_id: str, is_cart_enabled: bool,
                                    is_catalog_visible: bool) -> CatalogResult:
        _log.info("commerce_settings_set", extra={"provider": "console",
                                                  "cart": bool(is_cart_enabled),
                                                  "catalog_visible": bool(is_catalog_visible)})
        return CatalogResult(ok=True)
