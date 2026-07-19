"""notification-service — the internal contracts this service CONSUMES.

════════════════════════════════════════════════════════════════════════════════════
  DECLARE WHAT YOU CALL. The gate checks the call actually exists.
════════════════════════════════════════════════════════════════════════════════════

Every service already declares what it SERVES, in app/manifest.py. Nothing declared what anyone
CALLS — and that asymmetry is not cosmetic. It is why compliance-service could serve consent-check
for four phases with zero callers while every gate passed: a server with no clients has no unused
symbols, no failing test and no missing config. The absence is invisible from either side alone.

So this is the other half of the seam. safety:contract-callers reads it and refuses three things:

  * a path nobody serves        — drift. The producer renamed or removed it and we still "call" it.
  * a port that does not exist  — a declaration with no code behind it, which would recreate the
                                  original defect one level up: a promise that nothing keeps.
  * a served contract marked `"caller": "service"` that NOBODY declares — the original defect.

FORMAT: a PURE literal, read with ast.literal_eval, so the gate never imports (and therefore never
executes) a service to inspect it.

    service   who serves it. Must match a directory under services/.
    path      the exact path from THEIR manifest, placeholders included.
    port      the port file in app/ports/ that speaks this contract. The gate checks it exists,
              because a declaration without an adapter is a claim, not a call.
    why       what breaks if this call stops working. Written for whoever is holding the pager.
"""
from __future__ import annotations

from typing import Any

CONSUMES: list[dict[str, Any]] = [
    {
        "service": "compliance-service",
        "path": "/internal/compliance/consent-check",
        "port": "consent_gate",
        "why": "notifications mapped to a purpose (marketing) need a lawful basis before they are "
               "sent. If this call stops working the gate answers UNKNOWN and, under the default "
               "fail-closed posture, those notifications stop — transactional mail is unaffected "
               "because it is sent under contract rather than consent.",
    },
]
