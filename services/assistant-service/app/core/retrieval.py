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
        # Filled by attach_vectors() when an embedder is configured; empty means lexical only.
        self._vectors: list[list[float]] = []
        self._postings: dict[str, list[int]] = {}
        for i, tf in enumerate(self._tf):
            for term in tf:
                self._postings.setdefault(term, []).append(i)

    def __len__(self) -> int:
        return len(self._chunks)

    # ── semantic half (optional) ──────────────────────────────────────────────
    def attach_vectors(self, vectors: list[list[float]]) -> None:
        """Store one vector per chunk, in chunk order.

        Length is checked rather than trusted: a short batch would misalign every vector with the
        wrong chunk, and the result is not an obvious error but a subtly wrong ranking — the kind
        that reads as "the assistant is a bit poor" for months.
        """
        if len(vectors) != len(self._chunks):
            raise ValueError(
                f"got {len(vectors)} vectors for {len(self._chunks)} chunks — a misaligned vector "
                f"scores the wrong passage and looks like mediocre quality, not a bug")
        self._vectors = [_unit(v) for v in vectors]

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        return sum(x * y for x, y in zip(a, b))       # both already unit-length

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
               lang: str = "", limit: int = 8, min_score: float = 0.0,
               query_vector: list[float] | None = None, alpha: float = 1.0) -> list[Hit]:
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

        # HYBRID. Lexical is exact and will always find a product name, an error code or a field
        # label; semantic is fuzzy in both directions. Blending keeps exactness while adding the
        # ability to tell "phone number" from "VAT number" — the case no single threshold splits,
        # because one side wants a higher bar and the other a lower one.
        #
        # Both halves are normalised to [0,1] over the CANDIDATE set before blending: raw BM25 is
        # unbounded and cosine is not, so adding them directly would make the blend weight mean
        # something different for every query.
        # `alpha < 1.0` is a SHORT-CIRCUIT, not a behaviour rule: at alpha == 1.0 the blend below
        # reduces to the lexical score and is rescaled back onto it, so removing this clause
        # changes no result — confirmed by mutation. It stays because the common case (no
        # embedder configured) should not walk every candidate to compute a zero.
        if query_vector and self._vectors and alpha < 1.0:
            qv = _unit(query_vector)
            top = max(scores.values()) if scores else 0.0
            blended: dict[int, float] = {}
            for i in candidates:
                lex = (scores.get(i, 0.0) / top) if top > 0 else 0.0
                sem = max(0.0, self._cosine(qv, self._vectors[i])) if i < len(self._vectors) else 0.0
                combined = alpha * lex + (1.0 - alpha) * sem
                if combined > 0:
                    blended[i] = combined
            # Rescaled back onto the lexical range so a configured min_score keeps its meaning
            # rather than silently becoming stricter the moment embeddings are switched on.
            scores = {i: v * top for i, v in blended.items()} if top > 0 else blended

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


def _unit(v: list[float]) -> list[float]:
    """Normalise to unit length so cosine similarity is a plain dot product.

    Done once at store time rather than per comparison: an un-normalised vector makes similarity
    depend on magnitude, and magnitude differs between providers and even between models from one
    provider — so the same KB would rank differently for a reason nobody could see.
    """
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm else list(v)
