"""Which WhatsApp send drivers are billed to US, the platform — the ONE place that fact lives.

The distinction is structural, not a deployment choice, so it is a constant tied to the adapters:

  * meta        — the establishment holds its own Meta account; Meta bills the SHOP directly. We never
                  see the charge, so we record no cost unit for it.
  * threesixtydialog — a BSP (360dialog) fronts the Cloud API on the platform's account; Meta bills
                  360dialog, 360dialog bills US, and we pass it through. THIS is what the cost tracker
                  records units for, and (later) what the send-time wallet gate applies to.
  * console / webhook — no Meta charge at all.

Both the after-the-fact cost tracker and the send-time wallet gate scope on this same set, so a new
provider-paid BSP is enrolled in one edit here rather than drifting across two call sites.
"""
from __future__ import annotations

# A send through one of these drivers is paid by the platform (provider-paid / BSP), so its usage is
# ours to meter. E.164/rate concerns live on the operations side; this set is only "whose bill is it".
PROVIDER_BILLED_WHATSAPP_DRIVERS: frozenset[str] = frozenset({"threesixtydialog"})


def is_provider_billed(driver: str) -> bool:
    """True when a send via `driver` lands on the platform's own bill (so we meter its cost)."""
    return driver in PROVIDER_BILLED_WHATSAPP_DRIVERS
