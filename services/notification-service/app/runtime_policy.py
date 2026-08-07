"""The settings an operator may change at runtime, for this service.

DECLARING A KEY HERE IS A PROMISE THAT SOMETHING READS IT through RuntimeConfig at the point of
use. That promise is the entire point: the alternative — offering every settable field to the panel
— fails in the most convincing way available. The panel accepts the change, the audit log records
it with an actor and a timestamp, and behaviour does not move, because nothing consults the
override. The operator ends up holding a receipt for a change that never happened.

So the list is short and grows only when a read is wired alongside it. Anything absent is refused,
which is the safe direction: unavailable is honest, inert is not.
"""
from __future__ import annotations

RUNTIME_POLICY = frozenset({
    # Provider preference and failover order, resolved per send in core/provider_pool.py. This is
    # the knob an operator most needs after launch: when a gateway degrades, the fix is to reorder
    # the chain, and that cannot wait for a deployment.
    "EMAIL_CHAIN",
    "SMS_CHAIN",
    "WHATSAPP_CHAIN",
})
