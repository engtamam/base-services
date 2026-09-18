"""Payment-gateway adapter registry — build a PaymentGateway from a driver name + resolved config.

Each shop picks a driver and supplies its OWN sealed credentials; this maps the driver string to the
adapter class and constructs it with that shop's config. An unknown driver fails closed to null (a
clear refusal), never to a gateway that silently accepts nothing.
"""
from __future__ import annotations

from typing import Any

from app.adapters.payment.console import ConsoleGateway
from app.adapters.payment.null import NullGateway
from app.adapters.payment.tuwaiqpay import TuwaiqPayGateway
from app.ports.payment_gateway import PaymentGateway

_REGISTRY: dict[str, type] = {
    "console": ConsoleGateway,
    "null": NullGateway,
    "tuwaiqpay": TuwaiqPayGateway,
}

# The drivers a tenant may actually select (null is the internal fail-closed default, not a choice).
SELECTABLE_DRIVERS = tuple(d for d in _REGISTRY if d != "null")


def build_gateway(driver: str, config: dict[str, Any] | None = None) -> PaymentGateway:
    """Construct the adapter for `driver` from `config` (already-decrypted per-tenant credentials).
    An unknown driver returns the fail-closed NullGateway rather than raising, so a misconfigured
    tenant refuses payments with a clear reason instead of 500-ing the whole route."""
    cls = _REGISTRY.get((driver or "").strip().lower(), NullGateway)
    return cls(**(config or {}))
