# =============================================================================
# authz-service — CWE catalog (bundled MITRE CWE, self-contained)
# -----------------------------------------------------------------------------
# Resolves any CWE id to a name + type. Three distinct sections are kept apart so
# a Category/View can NEVER be reported as a Weakness (real flaw).
# =============================================================================
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "cwe.json")


class CweCatalog:
    def __init__(self, path: str = _CATALOG_PATH) -> None:
        with open(path, encoding="utf-8") as fh:
            raw: dict[str, Any] = json.load(fh)
        self._weaknesses = raw.get("weaknesses", {})
        self._categories = raw.get("categories", {})
        self._views = raw.get("views", {})
        self.version = raw.get("cwe_source", "unknown")

    def is_weakness(self, cwe_id: str) -> bool:
        return cwe_id in self._weaknesses

    def resolve(self, cwe_id: str) -> dict[str, str]:
        if cwe_id in self._weaknesses:
            return {"id": cwe_id, "name": self._weaknesses[cwe_id]["name"], "type": "weakness"}
        if cwe_id in self._categories:
            return {"id": cwe_id, "name": self._categories[cwe_id]["name"], "type": "category"}
        if cwe_id in self._views:
            return {"id": cwe_id, "name": self._views[cwe_id]["name"], "type": "view"}
        raise KeyError(f"unknown CWE id: {cwe_id}")

    def name(self, cwe_id: str) -> str:
        try:
            return self.resolve(cwe_id)["name"]
        except KeyError:
            return cwe_id


@lru_cache(maxsize=1)
def get_catalog() -> CweCatalog:
    return CweCatalog()
