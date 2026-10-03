"""Smart classification of a Fatoora response — the transient-vs-hard decision.

A ZATCA rejection is a REAL answer (don't blind-retry); a transient/network failure
is retryable (invoice stays 'pending' for the sweep). Ported from the studied refs.
"""
from __future__ import annotations

from typing import Any

# HTTP codes that mean "retry later" (throttled / server / transient).
# zatca-016: 401 is NOT a plain network transient — it is an AUTH failure (expired/wrong PCSID). It is
# still retryable (a CSID renewal recovers it), but it must be DIAGNOSED distinctly (auth_error flag +
# a credential-specific message) rather than hidden among the generic transients, so a bad CSID is seen.
_TRANSIENT = frozenset({408, 425, 429, 500, 502, 503, 504})
_AUTH = 401


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
    cleared_doc = resp.get("clearedInvoice")
    if success_status == "cleared":
        # CLEARANCE is a real success ONLY with BOTH a CLEARED status AND a non-empty ZATCA-stamped
        # cleared document (zatca-r2-08). The old rule accepted any body-less 2xx, marking a standard
        # invoice 'cleared' with cleared_xml='' — a terminal, silently non-compliant record.
        accepted = status_val == "CLEARED" and bool(cleared_doc)
        # A 2xx that did not clear (no status / CLEARED-without-document) is NOT hard-rejected: it is
        # incomplete/transient, so it stays retryable rather than terminal.
        incomplete_2xx = (200 <= code < 300 and not accepted
                          and "NOT_" not in status_val and not errors)
    else:
        accepted = status_val == "REPORTED" or (
            200 <= code < 300 and status_val != "" and "NOT_" not in status_val and not errors)
        # A body-less 2xx (no reportingStatus at all) is ambiguous, not success — retry it.
        incomplete_2xx = 200 <= code < 300 and not accepted and status_val == "" and not errors
    auth_error = code == _AUTH
    if accepted:
        outcome = success_status
    elif auth_error or code in _TRANSIENT or code == 0 or incomplete_2xx:  # transient/auth/ambiguous → retry
        outcome = "pending"
    else:                                       # 400 / 413 / NOT_* / 2xx-with-errors → hard reject
        outcome = "rejected"
    if outcome == success_status:
        message = None
    elif auth_error:
        # Distinct, diagnosable: a bad/expired PCSID, not a generic network blip.
        message = ("; ".join(errors)
                   or "HTTP 401 — ZATCA rejected the CSID/credential (expired or wrong PCSID); "
                      "renew the CSID. Retrying until then.")
    else:
        message = "; ".join(errors) or f"HTTP {code} {status_val}".strip()
    return {
        "outcome": outcome, "code": int(code), "auth_error": auth_error,
        "cleared_xml": resp.get("clearedInvoice"),
        "warnings": warnings, "errors": errors,
        "message": message,
    }
