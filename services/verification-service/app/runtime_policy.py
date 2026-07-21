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

RUNTIME_POLICY: frozenset[str] = frozenset({
    # Which provider serves each claim type — resolved per check in core/verifier_pool.py.
    # An operator repointing a claim at another provider is exactly the change that cannot wait
    # for a deployment.
    "VERIFY_PROVIDERS",
})
