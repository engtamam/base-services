"""Catalog KnowledgeSource — help built from what the product declares about itself.

A platform's services already declare the actions they expose: an id, a human label, the route, and
the capability required. If that is published as one aggregated, machine-readable document, then
"what can I do here?" has an answer that is generated rather than written — and, more importantly,
one that cannot go stale. Hand-written help drifts the moment a feature ships; help derived from
the declaration changes when the declaration changes, because it IS the declaration.

THIS IS NOT TIED TO ANY PARTICULAR PLATFORM. It reads a URL that returns

    {"services": [{"service": "...", "actions": [
        {"id": ..., "label": ..., "method": ..., "path": ..., "capability": ..., "group": ...}]}]}

so an adopter's own gateway serving that shape works identically to ours, and an adopter's own
services appear the moment they declare themselves. No service list lives here.

WHY THE FETCH USES A BROAD IDENTITY, AND WHY THAT IS SAFE. A caller-filtered catalog would give a
per-caller KB, which cannot be shared or indexed once. So the document is fetched with the
service's own identity and every action's `capability` becomes the entry's `requires`. Per-caller
filtering then happens where all the other filtering happens — at query time, in the index. The
entry exists for everyone; it is visible only to a caller who holds the capability. That is the
same guarantee, applied in one place instead of two.

GENERATED ENTRIES ARE MARKED. `source="catalog"` distinguishes them from authored ones, so a
regenerated catalog replaces only what it produced and never a sentence a human wrote.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Awaitable, Callable

from app.core.kb import ALL_SURFACES, KbEntry
from app.ports.errors import ConfigError
from app.ports.knowledge_source import KbSnapshot

# transport(url, headers) -> (status, json)
Transport = Callable[[str, dict], Awaitable[tuple[int, Any]]]


class CatalogKnowledgeSource:
    driver = "catalog"

    def __init__(self, url: str, token: str = "", *, timeout: float = 10.0,
                 transport: Transport | None = None, prefix: str = "action") -> None:
        if not url:
            raise ConfigError("KB_CATALOG_URL is required for the catalog knowledge source")
        self._url = url
        self._token = token
        self._timeout = timeout
        self._transport = transport or self._httpx_transport
        # Namespaced so a generated id can never collide with an authored one.
        self._prefix = prefix.strip(". ") or "action"

    async def load(self) -> KbSnapshot:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["X-Internal-Token"] = self._token
        try:
            status, body = await self._transport(self._url, headers)
        except Exception as exc:   # noqa: BLE001 — any transport failure is one condition here
            raise ConfigError(f"action catalog unreachable at {self._url}: {exc}") from exc
        if status != 200 or not isinstance(body, dict):
            # Loud. An empty catalog and an unreachable one both produce an assistant that cannot
            # answer "what can I do here?", and only one of them is a deployment mistake.
            raise ConfigError(f"action catalog returned HTTP {status} — refusing to serve a KB "
                              f"that would silently be missing every action")

        entries: list[KbEntry] = []
        for svc in body.get("services") or []:
            service_name = str(svc.get("service") or "").strip()
            for action in svc.get("actions") or []:
                entry = self._entry(service_name, action)
                if entry is not None:
                    entries.append(entry)

        return KbSnapshot(entries=tuple(entries),
                          generation=_digest(entries), driver=self.driver)

    def _entry(self, service: str, action: dict) -> KbEntry | None:
        action_id = str(action.get("id") or "").strip()
        label = str(action.get("label") or "").strip()
        if not action_id or not label:
            # An action without a label cannot be described to a human. Skipped rather than
            # rendered as its raw id, which reads like a leak of internals into help text.
            return None

        method = str(action.get("method") or "").strip().upper()
        path = str(action.get("path") or "").strip()
        group = str(action.get("group") or "").strip()
        cap = str(action.get("capability") or "").strip()

        where = f"{group} in {service}" if group and service else (group or service)
        body = label if not where else f"{label}.\n\nWhere: {where}."

        return KbEntry(
            id=f"{self._prefix}.{action_id}",
            title={"en": label},
            body={"en": body},
            kind="action",
            surfaces=(ALL_SURFACES,),
            # The capability the platform itself enforces on the route becomes the capability that
            # governs whether this help is even visible. One source, both meanings.
            requires=cap,
            source=self.driver,
            # Carried so an answer can deep-link to the action rather than describe where it is.
            metadata={"service": service, "method": method, "path": path,
                      "group": group, "action_id": action_id},
        )

    async def health(self) -> bool:
        try:
            status, _ = await self._transport(self._url, {"X-Internal-Token": self._token})
            return status == 200
        except Exception:   # noqa: BLE001
            return False

    async def close(self) -> None:
        return None

    async def _httpx_transport(self, url: str, headers: dict):   # pragma: no cover - needs a live gateway
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=headers)
        try:
            return resp.status_code, resp.json()
        except Exception:   # noqa: BLE001
            return resp.status_code, None


def _digest(entries: list[KbEntry]) -> str:
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda x: x.id):
        h.update(json.dumps({"id": e.id, "title": e.title, "body": e.body,
                             "requires": e.requires, "metadata": e.metadata},
                            sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:32]
