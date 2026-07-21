"""In-memory KnowledgeSource — the zero-infra default and the one tests use.

Not a stub: it is the real port, so a service booted with no configuration answers questions from
whatever entries it was handed. That keeps the zero-config path a working product rather than a
demo that 500s.
"""
from __future__ import annotations

import hashlib
import json

from app.core.kb import KbEntry, parse_kb
from app.ports.knowledge_source import KbSnapshot


class MemoryKnowledgeSource:
    driver = "memory"

    def __init__(self, entries: list[KbEntry] | None = None, raw: dict | list | None = None) -> None:
        if raw is not None:
            entries = parse_kb(raw, source=self.driver)
        self._entries = tuple(entries or ())
        self._generation = _digest(self._entries)

    async def load(self) -> KbSnapshot:
        return KbSnapshot(entries=self._entries, generation=self._generation, driver=self.driver)

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def _digest(entries: tuple[KbEntry, ...]) -> str:
    """Content hash over the entries. Sorted, so an ordering difference in the source is not
    mistaken for a content change and does not rebuild the index for nothing."""
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda x: x.id):
        h.update(json.dumps(
            {"id": e.id, "title": e.title, "body": e.body, "kind": e.kind,
             "surfaces": list(e.surfaces), "anchor": e.anchor, "requires": e.requires},
            sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:32]
