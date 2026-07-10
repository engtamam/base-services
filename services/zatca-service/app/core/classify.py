"""Smart classification of a Fatoora response — the transient-vs-hard decision.

A ZATCA rejection is a REAL answer (don't blind-retry); a transient/network failure
is retryable (invoice stays 'pending' for the sweep). Ported from the studied refs.
"""
from __future__ import annotations

from typing import Any

# HTTP codes that mean "retry later" (renew CSID / throttled / server / transient).
_TRANSIENT = frozenset({401, 408, 425, 429, 500, 502, 503, 504})


def messages(vr: dict, key: str) -> list[str]:
    """Flatten a validationResults message array to plain strings."""
    out: list[str] = []
    for m in (vr.get(key) or []):
        if isinstance(m, dict):
            txt = m.get("message") or m.get("code") or ""
            if txt:
                out.append(str(txt))
        elif m:
            out.append(str(m))
    return out


def classify(code: int, resp: dict, *, success_status: str) -> dict[str, Any]:
    """Normalize a reporting/clearance response. success_status = 'reported'|'cleared'.
    outcome ∈ {success_status, 'rejected' (hard), 'pending' (transient)}."""
    vr = resp.get("validationResults") or {}
    warnings = messages(vr, "warningMessages")
    errors = messages(vr, "errorMessages")
    status_val = str(resp.get("clearanceStatus") or resp.get("reportingStatus") or "").upper()
    accepted = status_val in ("CLEARED", "REPORTED") or (
        200 <= code < 300 and "NOT_" not in status_val and not errors)
    if accepted:
        outcome = success_status
    elif code in _TRANSIENT or code == 0:      # transient / re-onboard / network → retry
        outcome = "pending"
    else:                                       # 400 / 413 / NOT_* → hard reject
        outcome = "rejected"
    return {
        "outcome": outcome, "code": int(code),
        "cleared_xml": resp.get("clearedInvoice"),
        "warnings": warnings, "errors": errors,
        "message": None if outcome == success_status
        else ("; ".join(errors) or f"HTTP {code} {status_val}".strip()),
    }
