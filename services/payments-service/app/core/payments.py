"""PaymentService — the conduit's orchestration: create a pay-link, redirect, apply outcomes.

It never touches money. It uses the shop's OWN gateway (resolved from sealed MERCHANT_* creds) to
mint a hosted link, records a server-authoritative PaymentRequest, and — from a VERIFIED webhook or a
reconcile fetch — moves that request to a terminal state EXACTLY ONCE and emits `payment.completed`
(or `payment.failed`). Guarantees enforced here:

  * server-authoritative amount — a callback can only settle the amount we recorded; a mismatch is
    refused (marked failed + flagged), never completed;
  * idempotent apply — webhook and reconcile share this one seam; the terminal transition is a CAS,
    so a replay or a race yields one completion and one event;
  * open-redirect safe — the redirector only ever 302s to a host on the operator's allowlist
    (fail-closed: an empty allowlist refuses every redirect);
  * fail-closed — an unconfigured or locked tenant, an unverified callback, or an unreachable gateway
    never fabricates a "paid".
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.merchant_credentials import MerchantGateways
from app.core.payment_requests import COMPLETED, PaymentRequestStore
from app.ports.event_bus import Event, EventBus
from app.ports.payment_gateway import FAILED, SUCCEEDED, GatewayUnavailable, PaymentOutcome

TOPIC_COMPLETED = "payment.completed"
TOPIC_FAILED = "payment.failed"


class PaymentsDisabled(Exception):
    """The tenant is not permitted to take payments (locked, or the option is off). Fail-closed."""


class PaymentNotConfigured(Exception):
    """The tenant has no gateway configured — nothing to mint a link with."""


@dataclass
class CreatedLink:
    request_id: str
    pay_url: str            # OUR redirector token URL (what we hand the customer / put in a button)
    redirect_url: str       # the gateway's hosted page (behind the redirector)
    expires_at: float
    provider: str
    provider_ref: str


class PaymentService:
    def __init__(self, *, requests: PaymentRequestStore, gateways: MerchantGateways,
                 event_bus: EventBus, redirect_base_url: str, allowed_hosts: frozenset[str],
                 link_ttl_seconds: int, is_enabled=None) -> None:
        self._req = requests
        self._gw = gateways
        self._bus = event_bus
        self._redirect_base = redirect_base_url.rstrip("/")
        self._allowed_hosts = allowed_hosts
        self._ttl = int(link_ttl_seconds)
        # Optional async (tenant)->bool lock check. None ⇒ always enabled (wired in P6).
        self._is_enabled = is_enabled

    def _host_allowed(self, url: str) -> bool:
        # Fail-closed: an empty allowlist permits nothing. A redirect target must be an https URL whose
        # host is explicitly allowed — this is what makes the public redirector not an open redirect.
        host = (urlsplit(url).hostname or "").lower()
        return bool(host) and host in self._allowed_hosts

    async def _ensure_enabled(self, tenant: str) -> None:
        if self._is_enabled is not None and not await self._is_enabled(tenant):
            raise PaymentsDisabled(f"payments are disabled for tenant {tenant!r}")

    # ── create ──────────────────────────────────────────────────────────────────
    async def create_link(self, *, tenant: str, reference: str, amount_minor: int, currency: str,
                          metadata: dict | None = None, return_url: str = "") -> CreatedLink:
        """Mint (or re-use) a hosted pay-link for one order. Idempotent by (tenant, reference) AND
        safe under concurrency: an ATOMIC claim on (tenant, reference) is taken BEFORE the gateway
        bill is minted, so two concurrent (or retried) calls for the same order can never mint two
        bills / two links — exactly one wins, the others resolve to that same request."""
        await self._ensure_enabled(tenant)

        # Fast path: a still-valid or completed request already exists → return it, no claim needed.
        existing = await self._req.active_by_reference(tenant=tenant, reference=reference)
        if existing is not None:
            return self._to_link(existing)

        import uuid as _uuid
        # Bounded attempts: win the claim, or resolve to the winner, or steal a stale claim. The loop
        # terminates because each pass either mints (win), returns (winner resolved), or steals+mints.
        for _ in range(64):
            rid = _uuid.uuid4().hex
            won, claim = await self._req.claim(tenant=tenant, reference=reference, request_id=rid)
            if won:
                return await self._mint_claimed(
                    tenant=tenant, reference=reference, request_id=rid, amount_minor=amount_minor,
                    currency=currency, metadata=metadata, return_url=return_url)
            # Lost the claim — someone else is (or was) minting this order. Resolve to their request.
            resolved = await self._resolve_claim(tenant=tenant, reference=reference, claim=claim)
            if resolved is not None:
                return resolved
            # The claim is stale (its request expired/failed and left no usable link): steal it.
            if claim is not None:
                stolen_rid = _uuid.uuid4().hex
                if await self._req.steal_claim(claim, request_id=stolen_rid):
                    return await self._mint_claimed(
                        tenant=tenant, reference=reference, request_id=stolen_rid,
                        amount_minor=amount_minor, currency=currency, metadata=metadata,
                        return_url=return_url)
            # Lost the steal too — another caller is mid-mint; re-read on the next pass.
        raise GatewayUnavailable("could not acquire a payment claim for this order; retry")

    async def _resolve_claim(self, *, tenant: str, reference: str, claim: dict | None) -> CreatedLink | None:
        """Given a claim we did NOT win, return the winner's usable request (pending-not-expired or
        completed), waiting briefly for a mint in flight. Returns None if the claim is stale (its
        request is expired/failed/cancelled) so the caller may steal it."""
        import asyncio
        target_rid = (claim or {}).get("request_id", "")
        if not target_rid:
            return None
        for _ in range(50):                              # ~0.5s max, for a concurrent mint to land
            row = await self._req.get(target_rid)
            if row is not None:
                status = row.get("status")
                if status == COMPLETED or (status == "pending" and not self._req.is_expired(row)):
                    return self._to_link(row)
                if status in ("failed", "cancelled") or self._req.is_expired(row):
                    return None                          # stale → caller may steal the claim
                # status == provisioning (mint in flight) → wait and re-read
            await asyncio.sleep(0.01)
        return None

    async def _mint_claimed(self, *, tenant: str, reference: str, request_id: str, amount_minor: int,
                            currency: str, metadata: dict | None, return_url: str) -> CreatedLink:
        """Mint the gateway bill for a claim we won, then persist the request. Releases the claim on a
        gateway failure so a failed attempt never permanently blocks a retry for this order."""
        gateway = await self._gw.gateway_for(tenant)
        if getattr(gateway, "driver", "null") == "null":
            await self._req.release_claim(tenant=tenant, reference=reference)
            raise PaymentNotConfigured(f"tenant {tenant!r} has no payment gateway configured")
        try:
            session = gateway.prepare_checkout(
                amount_minor=amount_minor, currency=currency, reference=reference,
                return_url=return_url, metadata=metadata or {})
        except GatewayUnavailable:
            await self._req.release_claim(tenant=tenant, reference=reference)
            raise
        if not session.ok or not session.redirect_url:
            await self._req.release_claim(tenant=tenant, reference=reference)
            raise GatewayUnavailable(session.error or "gateway did not return a checkout link")
        row = await self._req.create(
            request_id=request_id, tenant=tenant, reference=reference, amount_minor=amount_minor,
            currency=currency, provider=session.provider, provider_ref=session.provider_ref,
            redirect_url=session.redirect_url, ttl_seconds=self._ttl,
            expires_at_provider=session.expires_at)
        return self._to_link(row)

    def _to_link(self, row: dict) -> CreatedLink:
        return CreatedLink(
            request_id=row["id"],
            pay_url=f"{self._redirect_base}/api/v1/payments/pay/{row['id']}" if self._redirect_base
            else row["redirect_url"],
            redirect_url=row["redirect_url"], expires_at=float(row.get("expires_at", 0)),
            provider=row.get("provider", ""), provider_ref=row.get("provider_ref", ""))

    # ── redirect ──────────────────────────────────────────────────────────────────
    async def redirect_target(self, request_id: str) -> str:
        """The gateway URL to 302 a customer to, or raise. Expires a stale pending request, refuses a
        terminal one, and host-allowlists the target (fail-closed) so this is never an open redirect."""
        row = await self._req.get(request_id)
        if row is None:
            raise KeyError("unknown payment request")
        if self._req.is_expired(row):
            await self._req.mark_expired(row)
            raise PaymentsDisabled("payment link has expired")
        if row.get("status") != "pending":
            raise PaymentsDisabled(f"payment link is no longer active ({row.get('status')})")
        url = row.get("redirect_url", "")
        if not self._host_allowed(url):
            # The gateway handed us a link whose host is not on the allowlist — refuse rather than
            # bounce a customer to an unvetted host. An operator adds the gateway's host to the list.
            raise PaymentsDisabled("payment link host is not allow-listed")
        return url

    # ── apply an outcome (shared by webhook + reconcile) ──────────────────────────
    async def apply_outcome(self, *, tenant: str, outcome: PaymentOutcome) -> dict | None:
        """Move the referenced request to terminal state once and emit the event. Returns the updated
        row, or None if it could not be resolved. Only a VERIFIED, amount-matching success completes."""
        if not outcome.verified:
            return None
        row = await self._req.by_provider_ref(tenant=tenant, provider_ref=outcome.provider_ref)
        if row is None:
            return None
        # Server-authoritative amount: a settled callback whose amount differs from what we recorded is
        # NOT a completion — it is refused and flagged, never a fabricated or wrong-sum "paid".
        mismatch = bool(outcome.amount_minor) and (
            int(outcome.amount_minor) != int(row["amount_minor"])
            or (outcome.currency and outcome.currency.upper() != str(row["currency"]).upper()))
        effective_status = FAILED if (outcome.status == SUCCEEDED and mismatch) else outcome.status
        transitioned, updated = await self._req.apply_outcome(
            row, status=effective_status, event_id=outcome.event_id,
            paid_amount_minor=outcome.amount_minor, paid_currency=outcome.currency,
            amount_mismatch=mismatch)
        if transitioned:
            await self._emit(updated, mismatch=mismatch)
        return updated

    async def _emit(self, row: dict, *, mismatch: bool) -> None:
        topic = TOPIC_COMPLETED if row.get("status") == COMPLETED else TOPIC_FAILED
        # NEVER include any credential or the redirect URL in an event — only the facts a consumer
        # needs to fulfil the order.
        payload = {
            "tenant": row.get("tenant", ""), "request_id": row.get("id", ""),
            "reference": row.get("reference", ""), "amount_minor": row.get("amount_minor", 0),
            "currency": row.get("currency", ""), "provider": row.get("provider", ""),
            "provider_ref": row.get("provider_ref", ""), "status": row.get("status", ""),
        }
        if mismatch:
            payload["amount_mismatch"] = True
        await self._bus.publish(topic, Event(type=topic, payload=payload))

    async def handle_webhook(self, *, tenant: str, headers: dict[str, str], body: bytes) -> dict | None:
        """Verify a gateway webhook with the tenant's own gateway, then apply it idempotently."""
        gateway = await self._gw.gateway_for(tenant)
        outcome = gateway.verify_webhook(headers=headers, body=body)
        return await self.apply_outcome(tenant=tenant, outcome=outcome)

    async def reconcile(self, *, tenant: str, request_id: str) -> dict | None:
        """Pull the current status from the gateway for a request whose webhook was missed, and apply
        it through the SAME idempotent seam. Never invents a status the gateway did not report."""
        row = await self._req.get(request_id)
        if row is None or row.get("tenant") != tenant:
            return None
        if row.get("status") != "pending":
            return row
        gateway = await self._gw.gateway_for(tenant)
        outcome = gateway.fetch_status(provider_ref=row.get("provider_ref", ""),
                                       currency=row.get("currency", ""))
        outcome.provider_ref = row.get("provider_ref", "")   # anchor to our stored ref
        return await self.apply_outcome(tenant=tenant, outcome=outcome)
