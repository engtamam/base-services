"""Composite KnowledgeSource — authored help and generated help in one KB.

The two kinds have to coexist: actions are generated from declarations and cannot go stale, while
glossary terms, field help and policies are written by a human and cannot be derived from anything.
Keeping them in separate KBs would mean two retrieval paths, two rank orders and two sets of
citations — and a question that spans both ("how do I pay, and what does 'past due' mean?") would
be answerable from neither.

ORDER DECIDES WHO WINS, AND AUTHORED HELP WINS. Sources are merged in configured order and the
FIRST id encountered is kept. Put the file source first and a human can override one generated
sentence without either forking the generator or losing the rest of it. The reverse — a regenerated
catalog silently replacing a sentence someone wrote deliberately — is the failure that makes teams
stop trusting generated content.

A FAILING SOURCE FAILS THE LOAD. Not "serve what we could reach": an assistant quietly missing
every action, or every glossary term, still answers confidently from what remains, and nothing in
the response says half its knowledge is absent.
"""
from __future__ import annotations

import hashlib

from app.ports.knowledge_source import KbSnapshot


class CompositeKnowledgeSource:
    driver = "composite"

    def __init__(self, sources: list) -> None:
        if not sources:
            raise ValueError("composite knowledge source needs at least one source")
        self._sources = list(sources)

    async def load(self) -> KbSnapshot:
        entries, seen, gens = [], set(), []
        for src in self._sources:
            snap = await src.load()            # raises on failure — deliberately not caught
            gens.append(f"{snap.driver}:{snap.generation}")
            for entry in snap.entries:
                if entry.id in seen:
                    continue                   # first source wins; authored beats generated
                seen.add(entry.id)
                entries.append(entry)
        gen = hashlib.sha256("|".join(gens).encode("utf-8")).hexdigest()[:32]
        return KbSnapshot(entries=tuple(entries), generation=gen, driver=self.driver)

    async def health(self) -> bool:
        for src in self._sources:
            if not await src.health():
                return False
        return True

    async def close(self) -> None:
        for src in self._sources:
            await src.close()
