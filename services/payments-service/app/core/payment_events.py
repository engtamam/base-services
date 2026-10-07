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

# Marketplace lifecycle — reserved for Phase 1+ (declared here when their emitters land, so the
# catalogue stays the single source of truth):
#   TOPIC_ONBOARDING_UPDATED = "merchant.onboarding_updated"
#   TOPIC_PAYOUT_SETTLED     = "payout.settled"
#   TOPIC_PAYOUT_FAILED      = "payout.failed"

# The full set this service may publish — one declared catalogue (mirrors billing-service's EMITTED).
EMITTED = (TOPIC_COMPLETED, TOPIC_FAILED)

__all__ = ["TOPIC_COMPLETED", "TOPIC_FAILED", "EMITTED"]
