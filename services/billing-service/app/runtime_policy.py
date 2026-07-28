"""The settings an operator may change at runtime, for this service.

DECLARING A KEY HERE IS A PROMISE THAT SOMETHING READS IT through RuntimeConfig at the point of
use. Offering every settable field to the panel instead fails in the most convincing way available:
the panel accepts the change, the audit log records it with an actor and a timestamp, and behaviour
does not move, because nothing consults the override. The operator ends up holding a receipt for a
change that never happened.

Add a key here in the same change that wires the read, never before. Anything absent is refused,
which is the safe direction — unavailable is honest, inert is not.
"""
from __future__ import annotations

# PAYMENT_GATEWAY_DRIVER — the active payment gateway, per tenant, changeable after launch. READ in
# app/api/v1/billing.py's build_billing (via RuntimeConfig) and applied by container.rebuild_gateway,
# which is the promise this file makes. A switch to a driver that is not configured fails CLOSED (a
# gateway whose charge raises GatewayUnavailable) rather than charging through the boot provider.
RUNTIME_POLICY: frozenset[str] = frozenset({"PAYMENT_GATEWAY_DRIVER", "BILLING_AUTO_REACTIVATE"})
