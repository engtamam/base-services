"""BM25 relevance ranking (pure).

BM25 = the standard bag-of-words relevance model: rewards term frequency with diminishing
returns (`k1`), normalises by document length (`b`), and weights rare terms higher (`idf`).
Per-field boosts let a title match outrank a body match. Ties break by id so ranking is a
total order (required for stable cursor pagination).
"""
from __future__ import annotations

import math

K1 = 1.5
B = 0.75


def idf(n_docs: int, doc_freq: int) -> float:
    # BM25's idf, always positive (the +1 avoids negatives for very common terms).
    return math.log(1 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))


def score(*, query_terms: list[str], weighted_tf: dict[str, float], doc_len: float,
          avg_doc_len: float, n_docs: int, doc_freq: dict[str, int]) -> float:
    """`weighted_tf` is per-term frequency already multiplied by its field boost."""
    if avg_doc_len <= 0:
        avg_doc_len = 1.0
    total = 0.0
    for term in set(query_terms):
        tf = weighted_tf.get(term, 0.0)
        if tf <= 0:
            continue
        denom = tf + K1 * (1 - B + B * doc_len / avg_doc_len)
        total += idf(n_docs, doc_freq.get(term, 0)) * (tf * (K1 + 1)) / denom
    return total


def rank(hits: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """Order by score desc, then id asc — a total order for stable pagination."""
    return sorted(hits, key=lambda h: (-h[0], h[1]))
