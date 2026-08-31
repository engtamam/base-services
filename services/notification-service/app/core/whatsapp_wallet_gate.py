"""The send-time wallet gate for PROVIDER-BILLED WhatsApp template sends.

The base app includes N free WhatsApp messages per period; past that, each message draws overage from
the shop's wallet, and a send is REFUSED once the wallet cannot cover it (fail closed, balance never
negative). This gates send() — business-initiated templates, the only billable kind — and NEVER
send_interactive(): an interactive message is a reply inside the 24h customer-service window, always
the free "service" category, so gating it would block a customer confirming an already-proposed booking.

Shape (reserve → send → settle), which makes failover correct for free:
  before_send: skip unless enabled AND the tenant's chain uses a provider-billed driver; read the
    tenant's period count + operations' policy; if within the allowance (or overage is 0) allow free;
    else RESERVE the overage from wallet-service, refusing on an insufficient balance.
  after_send: CONFIRM the hold only if the send actually went out on a provider-billed driver;
    otherwise RELEASE it (the send failed, or failed over to a free driver like console) — so a message
    that went out free is never charged. The per-period count increments only on a confirmed billed send.

Every failure direction errs toward UNDER-charging: a lost confirm lets the hold auto-expire (frees the
funds), and a lost increment only softens the free tier. The hard money guarantee is wallet-service's
atomic reserve — this gate can never overdraw a wallet.

INERT unless a wallet client, a pricing client, and a counter are all wired: an unconfigured deployment
sends exactly as before.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from app.core.provider_billing import is_provider_billed
from app.observability.logging import get_logger
from app.ports.wallet import INSUFFICIENT

_log = get_logger(__name__)

FAIL_CLOSED = "fail_closed"
FAIL_OPEN = "fail_open"

# Distinct delivery statuses for the two ways the gate stops a send — a wallet a shop can top up vs a
# gate that could not reach its dependencies — so a delivery log answers "why did this stop?" precisely
# (the same reason consent uses consent_denied distinct from suppressed).
STATUS_INSUFFICIENT = "wallet_insufficient"
STATUS_UNAVAILABLE = "wallet_unavailable"


@dataclass
class GateDecision:
    action: str            # "skip" | "free" | "reserved" | "refuse"
    hold_id: str = ""
    status: str = ""       # delivery status to record on a "refuse"
    reason: str = ""       # delivery error detail on a "refuse"


_SKIP = GateDecision("skip")
_FREE = GateDecision("free")


class WhatsAppWalletGate:
    def __init__(self, *, wallet=None, pricing=None, counter=None,
                 unknown: str = FAIL_CLOSED, clock=time.time, id_factory=None) -> None:
        self._wallet = wallet
        self._pricing = pricing
        self._counter = counter
        self._unknown = unknown if unknown in (FAIL_CLOSED, FAIL_OPEN) else FAIL_CLOSED
        self._clock = clock
        self._new_id = id_factory or (lambda: uuid.uuid4().hex)

    @property
    def enabled(self) -> bool:
        return self._wallet is not None and self._pricing is not None and self._counter is not None

    def _on_unknown(self, why: str) -> GateDecision:
        """Wallet- or operations-service could not be reached. Posture decides: fail_closed refuses
        (never send unpaid), fail_open sends (availability over billing precision)."""
        if self._unknown == FAIL_OPEN:
            _log.warning("whatsapp_wallet_gate_fail_open", extra={"why": why})
            return _FREE
        return GateDecision("refuse", status=STATUS_UNAVAILABLE, reason=f"wallet_gate_unavailable:{why}")

    async def before_send(self, *, tenant: str, drivers) -> GateDecision:
        if not self.enabled or not any(is_provider_billed(d) for d in (drivers or [])):
            return _SKIP                        # not our bill to gate, or gate not configured
        now = self._clock()
        policy = await self._pricing.get(tenant)
        if policy is None:
            return self._on_unknown("pricing_unreachable")
        included = int(policy.get("included_messages_per_period", 0))
        overage = int(policy.get("overage_halalas_per_message", 0))
        count = await self._counter.count(tenant, now=now)
        if count < included or overage <= 0:
            return _FREE                        # within the allowance, or free past the cap (overage 0)
        res = await self._wallet.reserve(
            tenant=tenant, amount=overage, source=f"wa:{tenant}:{self._new_id()}",
            reason="whatsapp_overage")
        if res.ok:
            return GateDecision("reserved", hold_id=res.hold_id)
        if res.refused == INSUFFICIENT:
            return GateDecision("refuse", status=STATUS_INSUFFICIENT,
                                reason="insufficient_wallet_balance")
        return self._on_unknown("wallet_unavailable")

    async def after_send(self, decision: GateDecision, *, tenant: str, sent_ok: bool,
                         winning_driver: str) -> None:
        billed = is_provider_billed(winning_driver)
        if decision.action == "reserved":
            try:
                if sent_ok and billed:
                    await self._wallet.confirm(hold_id=decision.hold_id)
                else:
                    # send failed, or fell over to a non-billed driver (console/webhook) — free it.
                    await self._wallet.release(hold_id=decision.hold_id)
            except Exception:  # noqa: BLE001 — a settle failure lets the hold auto-expire (frees funds)
                _log.warning("whatsapp_wallet_settle_failed", extra={"hold": decision.hold_id})
        if sent_ok and billed and self._counter is not None:
            try:
                await self._counter.increment(tenant, now=self._clock())
            except Exception:  # noqa: BLE001 — a lost increment softens the free tier, never money
                pass
