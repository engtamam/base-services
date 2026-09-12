"""HyperLogLog (with an exact sparse mode) — a MERGEABLE unique-count sketch.

Why this exists: **you cannot add uniques together.** If Monday had 100 unique users and
Tuesday had 100, the two-day unique count is somewhere between 100 and 200 — never 200.
A stored `daily_unique_count` is therefore useless for a weekly figure, and every analytics
system that stores one is quietly reporting the wrong number.

A sketch fixes this: it is a *set-like* object you can **union** across buckets, then
estimate once at the end. Union is associative, commutative and idempotent (a monoid), so
rollups compose correctly over any time range and any group-by.

Two representations, chosen automatically:

  • **sparse** — a plain list of distinct hashes, while there are few of them. `estimate` is
    then the exact count. This matters: most rollup cells (a dimension value in one hour) hold
    a handful of users, and telling a small tenant it had 494 daily actives instead of 500 is
    indistinguishable from a bug. It is also *smaller* than the dense form at low cardinality.
  • **dense** — 2^p registers, once the sparse form would cost more than the dense one.
    Estimation is approximate (~1.6% at p=12) but memory is flat no matter the cardinality.

Promotion is one-way (sparse → dense) and happens inside `add`/`merge`, so callers never
choose. Both forms round-trip through JSON in the rollup row.
"""
from __future__ import annotations

import hashlib
import math

# 2^precision registers. p=12 → 4096 registers, ~1.6% standard error.
DEFAULT_PRECISION = 12
_MAX_HASH_BITS = 64


def _sparse_max(p: int) -> int:
    """Stay exact until the hash list would outgrow the dense registers it replaces.

    Derived from the configured precision rather than hardcoded: a dense sketch is 2^p small
    ints, so we allow 2^p/4 full-width hashes before promoting.
    """
    return max(1, (1 << p) >> 2)


def _alpha(m: int) -> float:
    if m == 16:
        return 0.673
    if m == 32:
        return 0.697
    if m == 64:
        return 0.709
    return 0.7213 / (1.0 + 1.079 / m)


def new(precision: int = DEFAULT_PRECISION) -> dict:
    """A fresh sketch, in exact sparse mode."""
    p = max(4, min(16, int(precision)))
    return {"p": p, "s": []}


def _hash64(value: str) -> int:
    return int.from_bytes(hashlib.sha1(str(value).encode()).digest()[:8], "big")  # nosec B324 - not crypto


def is_sparse(sketch: dict) -> bool:
    return "s" in sketch and sketch["s"] is not None


def _rank(h: int, p: int) -> tuple[int, int]:
    idx = h >> (_MAX_HASH_BITS - p)                      # first p bits pick the register
    remaining = (h << p) & ((1 << _MAX_HASH_BITS) - 1)   # the rest decides the run length
    rank = 1
    while rank <= (_MAX_HASH_BITS - p) and not (remaining >> (_MAX_HASH_BITS - rank)) & 1:
        rank += 1
    return idx, rank


def _densify(sketch: dict) -> dict:
    """One-way promotion: exact hashes → registers."""
    if not is_sparse(sketch):
        return sketch
    p = int(sketch["p"])
    regs = [0] * (1 << p)
    for h in sketch["s"]:
        idx, rank = _rank(int(h), p)
        if rank > regs[idx]:
            regs[idx] = rank
    return {"p": p, "r": regs}


def add(sketch: dict, value: str) -> dict:
    p = int(sketch["p"])
    h = _hash64(value)
    if is_sparse(sketch):
        hashes = sketch["s"]
        if h not in hashes:                              # set semantics ⇒ add is idempotent
            hashes.append(h)
            if len(hashes) > _sparse_max(p):
                return _densify(sketch)
        return sketch
    idx, rank = _rank(h, p)
    regs = sketch["r"]
    if rank > regs[idx]:
        regs[idx] = rank                                 # max ⇒ add is idempotent
    return sketch


def _fold_dense(regs: list, p: int, q: int) -> list:
    """Reduce a dense sketch from precision p to q<p (the standard HLL fold). Register j maps to
    q-register j>>(p-q); the (p-q) low bits of j become the LEADING bits of the folded run, so the
    folded rank is those bits' leading-zero count when they are non-zero, else the old rank + (p-q)."""
    d = p - q
    out = [0] * (1 << q)
    for j, val in enumerate(regs):
        low = j & ((1 << d) - 1)
        if low:
            rank = d - low.bit_length() + 1
        elif val > 0:
            rank = int(val) + d
        else:
            rank = 0
        qi = j >> d
        if rank > out[qi]:
            out[qi] = rank
    return out


def fold(sketch: dict, target_p: int) -> dict:
    """Down-convert a sketch to precision `target_p` (never up — precision cannot be added). Sparse
    stores full 64-bit hashes independent of p, so folding it is a relabel; dense folds its registers."""
    p = int(sketch["p"])
    q = max(4, min(16, int(target_p)))
    if q >= p:
        return sketch
    if is_sparse(sketch):
        return {"p": q, "s": list(sketch["s"])}
    return {"p": q, "r": _fold_dense(sketch["r"], p, q)}


def merge(a: dict, b: dict) -> dict:
    """Union two sketches. THE operation that makes uniques composable across buckets.

    If the two carry different precision (an operator raised HLL_PRECISION after some rollups were
    already written), DOWN-CONVERT both to the lower precision rather than raising — a query whose
    range spans the change must still answer, not 500. HLL_PRECISION is also a frozen product
    identifier, so this only arises for data written before the freeze."""
    if a is None:
        return b
    if b is None:
        return a
    p = min(int(a["p"]), int(b["p"]))
    a = fold(a, p)
    b = fold(b, p)

    if is_sparse(a) and is_sparse(b):
        union = list(dict.fromkeys([*a["s"], *b["s"]]))   # exact, order-stable
        if len(union) <= _sparse_max(p):
            return {"p": p, "s": union}
        return _densify({"p": p, "s": union})             # too big to stay exact

    da, db = _densify(a), _densify(b)
    return {"p": p, "r": [max(x, y) for x, y in zip(da["r"], db["r"])]}


def estimate(sketch: dict) -> int:
    if not sketch:
        return 0
    if is_sparse(sketch):
        return len(sketch["s"])                          # exact — no estimator error at all
    regs = sketch["r"]
    m = len(regs)
    zeros = regs.count(0)
    if zeros:                                            # linear counting for small cardinalities
        est = m * math.log(m / zeros)
        if est <= 2.5 * m:
            return int(round(est))
    raw = _alpha(m) * m * m / sum(2.0 ** -r for r in regs)
    return int(round(raw))


def merge_all(sketches: list[dict]) -> dict | None:
    out: dict | None = None
    for s in sketches:
        if out is None:
            out = {"p": int(s["p"]), "s": list(s["s"])} if is_sparse(s) else \
                  {"p": int(s["p"]), "r": list(s["r"])}
            continue
        out = merge(out, s)
    return out
