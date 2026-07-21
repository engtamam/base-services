"""Ranked retrieval over the KB — the reason prompt size stops tracking KB size.

Putting the whole KB in the prompt is the tempting shortcut, and the arithmetic kills it: a help KB
big enough to be useful for an admin console runs to hundreds of entries and hundreds of thousands
of characters, which is six figures of tokens on every single message. The shortcut then forces a
byte cap on the KB, so the product's help is frozen at whatever fits the budget.

Retrieval inverts that. We rank, keep the best few passages, and spend a few thousand tokens
whether the KB holds fifty entries or fifty thousand.

BM25, NOT RAW TERM COUNTS. Two properties matter here. Saturation: a passage repeating "invoice"
nine times is not nine times more about invoices, and without saturation a single repetitive entry
dominates every billing question. Length normalisation: a long page would otherwise beat a short
precise one purely by containing more words. Both are why the ranking is BM25 rather than the
obvious count.

FILTERS RUN BEFORE SCORING, NOT AFTER. Two reasons, and the second is a security property:

  * IDF is computed over the candidate set. Filtering afterwards would score against a corpus the
    caller cannot see, so a term common in admin help would look rare — and therefore highly
    significant — to a customer.
  * A relevance threshold compared against scores from invisible documents is not the threshold
    the operator configured.

Capability filtering here is defence in depth, not the only guard: entries also carry `requires`
and the answer layer re-checks. A retrieval bug should not be sufficient to leak admin help.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from app.core.indexing import Chunk, tokenize
from app.core.kb import KbEntry

# BM25 constants. k1 controls how fast term frequency saturates, b how strongly length is
# normalised. These are the long-standing defaults; they are config-surfaced rather than tuned here
# because the right values depend on a corpus we do not have yet.
DEFAULT_K1 = 1.2
DEFAULT_B = 0.75


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float
    entry: KbEntry


class KbIndex:
    """An inverted index over chunks, built once per KB generation.

    Rebuilt only when the source's generation changes, so an unchanged KB costs nothing per
    request. The index is shared across callers, which is exactly why no caller-specific filtering
    is baked into it — that happens at query time against the caller's own surface and capabilities.
    """

    def __init__(self, entries: tuple[KbEntry, ...] | list[KbEntry], chunks: list[Chunk],
                 *, generation: str = "", k1: float = DEFAULT_K1, b: float = DEFAULT_B) -> None:
        self.generation = generation
        self._k1, self._b = k1, b
        self._entries = {e.id: e for e in entries}
        self._chunks = chunks
        self._tf: list[Counter[str]] = [Counter(c.tokens) for c in chunks]
        self._len = [max(1, len(c.tokens)) for c in chunks]
        self._avg_len = (sum(self._len) / len(self._len)) if self._len else 1.0
        # term -> chunk ordinals containing it
        self._postings: dict[str, list[int]] = {}
        for i, tf in enumerate(self._tf):
            for term in tf:
                self._postings.setdefault(term, []).append(i)

    def __len__(self) -> int:
        return len(self._chunks)

    def _candidates(self, *, surface: str, caps: frozenset[str], lang: str) -> list[int]:
        """Chunk ordinals this caller may see, in this language, on this surface."""
        out = []
        for i, chunk in enumerate(self._chunks):
            entry = self._entries.get(chunk.entry_id)
            if entry is None:
                continue
            if not entry.visible_to(caps):
                continue
            if not entry.serves(surface):
                continue
            if lang and chunk.lang != lang and not self._only_language(chunk.entry_id, lang):
                continue
            out.append(i)
        return out

    def _only_language(self, entry_id: str, lang: str) -> bool:
        """True when the entry has no chunk in `lang`, so its other language may still answer.

        Otherwise an entry written only in Arabic is invisible to an English question even though
        it holds the answer — and the assistant reports not knowing something it knows.
        """
        entry = self._entries.get(entry_id)
        if entry is None:
            return False
        return lang not in (set(entry.title) | set(entry.body))

    def search(self, query: str, *, surface: str = "", caps: frozenset[str] = frozenset(),
               lang: str = "", limit: int = 8, min_score: float = 0.0) -> list[Hit]:
        """The best passages for `query`, already filtered to what this caller may see.

        Returns at most one chunk per entry: three passages of one page crowd out three different
        pages, and an answer assembled from one source is exactly what "grounded" is supposed to
        prevent.
        """
        terms = tokenize(query)
        if not terms:
            return []
        candidates = self._candidates(surface=surface, caps=caps, lang=lang)
        if not candidates:
            return []

        n = len(candidates)
        candidate_set = set(candidates)
        scores: dict[int, float] = {}
        for term in set(terms):
            posting = [i for i in self._postings.get(term, ()) if i in candidate_set]
            if not posting:
                continue
            # IDF over the CANDIDATE set — see the module docstring. Smoothed so a term present in
            # every candidate scores ~0 rather than negative.
            idf = math.log(1 + (n - len(posting) + 0.5) / (len(posting) + 0.5))
            for i in posting:
                tf = self._tf[i][term]
                norm = tf + self._k1 * (1 - self._b + self._b * self._len[i] / self._avg_len)
                scores[i] = scores.get(i, 0.0) + idf * (tf * (self._k1 + 1)) / norm

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], self._chunks[kv[0]].ref))
        hits: list[Hit] = []
        seen_entries: set[str] = set()
        for i, score in ranked:
            if score < min_score:
                break                      # sorted: everything after is also below
            chunk = self._chunks[i]
            if chunk.entry_id in seen_entries:
                continue
            seen_entries.add(chunk.entry_id)
            hits.append(Hit(chunk=chunk, score=score, entry=self._entries[chunk.entry_id]))
            if len(hits) >= limit:
                break
        return hits

    def by_anchor(self, anchor: str, *, surface: str = "",
                  caps: frozenset[str] = frozenset()) -> KbEntry | None:
        """The entry a UI element points at — the `?` icon path.

        No ranking, no model, no cost: this is a dictionary lookup, and it is the common case. It
        still applies both filters, because a direct lookup is not an authorisation bypass.
        """
        target = (anchor or "").strip()
        if not target:
            return None
        for entry in self._entries.values():
            if entry.anchor == target and entry.visible_to(caps) and entry.serves(surface):
                return entry
        return None


def budgeted(hits: list[Hit], *, max_chars: int) -> list[Hit]:
    """Keep hits in rank order until the character budget is spent.

    A budget rather than a fixed count, because eight short passages and eight long ones are very
    different prompts. Cutting whole chunks rather than truncating one keeps every citation
    checkable — a clipped passage can drop the very sentence the answer leaned on.
    """
    out, used = [], 0
    for hit in hits:
        cost = len(hit.chunk.text)
        if out and used + cost > max_chars:
            break
        out.append(hit)
        used += cost
    return out
