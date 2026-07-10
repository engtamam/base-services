"""PIH/ICV tamper-evident chain — genesis + first-break-wins verification.

The chain mirrors the audit-service hash-chain, ZATCA-shaped: ICV is a monotonic
counter (from 1); PIH of invoice N = the invoice hash of N-1; the first uses the
ZATCA genesis PIH. verify walks from genesis and reports the FIRST break.
"""
from __future__ import annotations

# ZATCA genesis PIH: base64 of the SHA-256 HEX string of "0" (matches the SDK refs).
GENESIS_PIH = (
    "NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOQ=="
)

# Statuses that count as "not yet finalized" — allowed only as the chain TAIL.
_UNSIGNED = frozenset({"none", "pending"})


def verify_chain(links: list[dict], *, genesis_pih: str = GENESIS_PIH) -> dict:
    """links: [{icv, pih, hash, status}] in any order. Returns
    {ok, count, break_at, reason}. Empty chain is ok."""
    ordered = sorted(links, key=lambda l: int(l["icv"]))
    n = len(ordered)
    prev_hash = genesis_pih
    for i, link in enumerate(ordered):
        icv = int(link["icv"])
        if icv != i + 1:                                   # gapless from 1
            return _break(icv, "icv_gap")
        if str(link.get("pih") or "") != prev_hash:        # linkage
            return _break(icv, "genesis_mismatch" if i == 0 else "pih_mismatch")
        is_tail = i == n - 1
        if not is_tail and str(link.get("status") or "") in _UNSIGNED:
            return _break(icv, "unsigned_link")
        prev_hash = str(link.get("hash") or "")
    return {"ok": True, "count": n, "break_at": None, "reason": None}


def _break(icv: int, reason: str) -> dict:
    return {"ok": False, "count": None, "break_at": icv, "reason": reason}
