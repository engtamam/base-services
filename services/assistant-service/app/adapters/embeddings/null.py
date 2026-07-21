"""Null embeddings — the default. Semantic scoring is off and says so.

Not a stub that returns zeros: `enabled` is False and the index skips the semantic half entirely.
A zero-vector embedder would make every cosine similarity identical, which is not "no semantic
signal" — it is a constant added to every score, quietly changing the ranking for no reason.
"""
from __future__ import annotations


class NullEmbeddings:
    driver = "null"
    dimensions = 0
    enabled = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return []

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None
