"""The payments-service event topics, declared in ONE place.

They were string literals inside `payments.py`; centralising them gives the service a single catalogue
to grow (the marketplace model adds payout/onboarding topics here, not scattered across modules) and
one list a manifest/consumer can read. `payments.py` re-exports the two it emits today, so every
existing import (`from app.core.payments import TOPIC_COMPLETED`) keeps working unchanged.

A consumer subscribes to these on the shared event bus; the payload carries only the facts needed to
fulfil the order, never a credential or the gateway redirect URL (see PaymentService._emit).
"""
from __future__ import annotations

# Conduit lifecycle — emitted today.
TOPIC_COMPLETED = "payment.completed"
TOPIC_FAILED = "payment.failed"

# Marketplace lifecycle.
# A sub-merchant's onboarding status changed (registered / approved / rejected) — a consumer uses it to
# prompt the shop ("finish verification", "you can now receive payouts"). Carries NO KYB/PII, only the
# tenant, the new status, and whether a payout destination is now on file.
TOPIC_ONBOARDING_UPDATED = "merchant.onboarding_updated"
# Payout (settlement) outcomes — reserved for Phase 2 with the payout reconcile sweep that emits them:
#   TOPIC_PAYOUT_SETTLED = "payout.settled"
#   TOPIC_PAYOUT_FAILED  = "payout.failed"

# The full set this service may publish — one declared catalogue (mirrors billing-service's EMITTED).
EMITTED = (TOPIC_COMPLETED, TOPIC_FAILED, TOPIC_ONBOARDING_UPDATED)

__all__ = ["TOPIC_COMPLETED", "TOPIC_FAILED", "TOPIC_ONBOARDING_UPDATED", "EMITTED"]
