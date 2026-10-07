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

import asyncio
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.merchant_credentials import CredentialResolver
from app.core.payment_requests import COMPLETED, EXPIRED, PaymentRequestStore
from app.ports.event_bus import Event, EventBus
from app.ports.payment_gateway import (CANCELLED, CAP_CHECKOUT, FAILED, MODEL_CONDUIT, PENDING,
                                       SUCCEEDED, GatewayUnavailable, PaymentOutcome, capabilities_of)

# Topics live in one catalogue (app.core.payment_events); re-exported here so existing imports
# (`from app.core.payments import TOPIC_COMPLETED`) keep working unchanged.
from app.core.payment_events import TOPIC_COMPLETED, TOPIC_FAILED  # noqa: E402,F401

_log = logging.getLogger("app.payments")


def _peek_provider_ref(body: bytes) -> str:
    """Cheaply pull the gateway's transaction id from a webhook body WITHOUT trusting it — used only
    to skip the outbound verify-by-fetch for a junk/unknown id (M2). Returns "" when the body does not
    plainly expose a ref, so the caller falls through to full verification (this never weakens the
    gate; it only avoids an outbound call that could not have settled anything)."""
    try:
        payload = json.loads(body.decode("utf-8", "replace")) if body else {}
    except Exception:   # noqa: BLE001 — an unparseable body exposes no ref; fall through to verify
        return ""
    if not isinstance(payload, dict):
        return ""
    details = payload.get("transactionDetails")
    if isinstance(details, dict) and details.get("transactionId"):
        return str(details["transactionId"])              # tuwaiqpay webhook shape
    for key in ("provider_ref", "transactionId"):         # console / generic shapes
        if payload.get(key):
            return str(payload[key])
    return ""


class PaymentsDisabled(Exception):
    """The tenant is not permitted to take payments (locked, or the option is off). Fail-closed."""


class RequestConflict(Exception):
    """A create for a reference that already has an ACTIVE request with a DIFFERENT amount/currency.
    Returning the stale link would bill the old amount for an order that was edited (audit FIN-017);
    the caller must settle/expire the old one or use a new reference. Fail-loud, never silently stale."""


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
    def __init__(self, *, requests: PaymentRequestStore, gateways: CredentialResolver,
                 event_bus: EventBus, redirect_base_url: str, allowed_hosts: frozenset[str],
                 link_ttl_seconds: int, is_enabled=None, select_mode=None) -> None:
        self._req = requests
        self._gw = gateways
        self._bus = event_bus
        self._redirect_base = redirect_base_url.rstrip("/")
        self._allowed_hosts = allowed_hosts
        self._ttl = int(link_ttl_seconds)
        # Optional async (tenant)->bool lock check. None ⇒ always enabled (wired in P6).
        self._is_enabled = is_enabled
        # Optional async (tenant)->str payment-mode resolver (the single-select PaymentModeGate). None,
        # or a resolver that returns the default, means MODEL_CONDUIT — today's only wired model — so an
        # untouched deployment behaves exactly as before. A tenant switched to a model this build does not
        # yet route (marketplace, until its path lands) is refused clearly rather than billed the wrong way.
        self._select_mode = select_mode

    def _host_allowed(self, url: str) -> bool:
        # Fail-closed: an empty allowlist permits nothing. A redirect target must be an HTTPS URL whose
        # host is explicitly allowed. The scheme check is load-bearing (M5): without it an
        # `http://<allow-listed-host>` downgrade would pass — the host being right does not make a
        # plaintext hop safe. Both together are what make the public redirector not an open redirect.
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return parts.scheme == "https" and bool(host) and host in self._allowed_hosts

    async def _ensure_enabled(self, tenant: str) -> None:
        if self._is_enabled is not None and not await self._is_enabled(tenant):
            raise PaymentsDisabled(f"payments are disabled for tenant {tenant!r}")

    async def _resolve_mode(self, tenant: str) -> str:
        """The tenant's single-select payment model. Defaults to MODEL_CONDUIT (today's only wired model),
        so no resolver — or a resolver returning the default — keeps behaviour byte-identical. A tenant
        set to a model this build does not yet route is refused fail-closed (never billed the wrong way)."""
        if self._select_mode is None:
            return MODEL_CONDUIT
        mode = await self._select_mode(tenant) or MODEL_CONDUIT
        if mode != MODEL_CONDUIT:
            # The marketplace routing branch lands later in Phase 1; until then a non-conduit selection is
            # a clear refusal, not a silent conduit charge.
            raise PaymentNotConfigured(
                f"payment mode {mode!r} is selected for tenant {tenant!r} but is not available yet")
        return mode

    async def get(self, request_id: str) -> dict | None:
        """The stored PaymentRequest row (or None), for a route that needs to read request state
        without reaching into the private store (I4)."""
        return await self._req.get(request_id)

    # ── create ──────────────────────────────────────────────────────────────────
    async def create_link(self, *, tenant: str, reference: str, amount_minor: int, currency: str,
                          metadata: dict | None = None, return_url: str = "") -> CreatedLink:
        """Mint (or re-use) a hosted pay-link for one order. Idempotent by (tenant, reference) AND
        safe under concurrency: an ATOMIC claim on (tenant, reference) is taken BEFORE the gateway
        bill is minted, so two concurrent (or retried) calls for the same order can never mint two
        bills / two links — exactly one wins, the others resolve to that same request."""
        await self._ensure_enabled(tenant)
        # Resolve the tenant's single-select payment model up front (defaults to conduit) — fail-closed on
        # a model this build cannot route yet, before any claim is taken or gateway bill is minted.
        await self._resolve_mode(tenant)

        # Fast path: a still-valid or completed request already exists → return it, no claim needed.
        # But it must be the SAME order: reusing a link whose amount/currency differs from this request
        # would bill the stale amount for an edited order (FIN-017). Refuse loudly instead.
        existing = await self._req.active_by_reference(tenant=tenant, reference=reference)
        if existing is not None:
            if (int(existing.get("amount_minor", -1)) != int(amount_minor)
                    or str(existing.get("currency", "")).upper() != currency.upper()):
                raise RequestConflict(
                    f"reference {reference!r} already has an active pay-link for a different "
                    f"amount/currency; settle or expire it, or use a new reference")
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
        # Fail-closed capability assertion (the extensibility contract): only mint a checkout on a
        # gateway that DECLARES it can. Every conduit adapter declares CAP_CHECKOUT, so this never fires
        # today — it is the seam that, once a marketplace adapter without a plain checkout exists, refuses
        # a checkout against it clearly instead of making a half-supported call. The null gateway (which
        # declares nothing) is already handled above, so this only ever sees a real adapter.
        if CAP_CHECKOUT not in capabilities_of(gateway):
            await self._req.release_claim(tenant=tenant, reference=reference)
            raise PaymentNotConfigured(
                f"tenant {tenant!r} gateway {getattr(gateway, 'driver', '')!r} does not support checkout")
        try:
            # OFFLOAD the blocking gateway HTTP off the event loop (payments-001): prepare_checkout
            # does a synchronous http.client round trip with a 30s timeout.
            session = await asyncio.to_thread(
                gateway.prepare_checkout,
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
        terminal one, and host-allowlists the target (fail-closed) so this is never an open redirect.

        This does NOT fully hide whether an id exists (I2): an unknown id raises KeyError, an
        existing-but-unusable one raises PaymentsDisabled — a link holder can still tell the two apart
        by the route's status code. The PaymentsDisabled message here is deliberately generic (it never
        names the concrete state) so those two are the ONLY distinguishable outcomes, leaving a future
        route free to collapse both to one response without changing this method."""
        row = await self._req.get(request_id)
        if row is None:
            raise KeyError("unknown payment request")
        if self._req.is_expired(row):
            await self._req.mark_expired(row)
            raise PaymentsDisabled("payment link is not available")
        if row.get("status") != PENDING:
            # Generic on purpose: do not leak the concrete terminal state (completed/failed/…) to a
            # link holder — every non-pending request refuses identically.
            raise PaymentsDisabled("payment link is not available")
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
        prior_status = row.get("status")
        # Server-authoritative amount. A verified success must carry a CONFIRMABLE amount that matches
        # what we recorded; anything else is refused (marked failed + flagged), never a fabricated or
        # wrong-sum "paid". A 0/absent amount on a SUCCEEDED is UNVERIFIABLE, not "matches" (H3) — we
        # can only refuse it, never read "no amount" as "amount matches". The currency check stays
        # reachable whenever an amount is present. A non-success outcome carries no amount claim.
        if outcome.status == SUCCEEDED:
            amount_ok = bool(outcome.amount_minor) and int(outcome.amount_minor) == int(row["amount_minor"])
            # A SUCCEEDED with NO confirmable currency is UNVERIFIABLE, not "matches" — the same H3
            # principle as the amount (never read "no value" as "value matches"). Since the amount
            # check is a bare minor-unit compare and SAR/USD/EUR all share exponent 2, an absent
            # currency would let a success in one currency complete a request recorded in another.
            currency_ok = bool(outcome.currency) and outcome.currency.upper() == str(row["currency"]).upper()
            mismatch = not (amount_ok and currency_ok)
            if not outcome.amount_minor or not outcome.currency:
                _log.warning("payment.unverifiable_outcome: verified success with no confirmable "
                             "amount/currency refused for request %s (tenant=%s, provider_ref=%s)",
                             row.get("id", ""), tenant, outcome.provider_ref)
        else:
            mismatch = False
        effective_status = FAILED if (outcome.status == SUCCEEDED and mismatch) else outcome.status
        transitioned, updated = await self._req.apply_outcome(
            row, status=effective_status, event_id=outcome.event_id,
            paid_amount_minor=outcome.amount_minor, paid_currency=outcome.currency,
            amount_mismatch=mismatch)
        if transitioned:
            # H2/M4: a verified success that had to override our own EXPIRED (or a prior failed/
            # cancelled) is money we nearly dropped — surface it loudly so ops confirm fulfilment.
            if effective_status == SUCCEEDED and prior_status in (EXPIRED, FAILED, CANCELLED):
                _log.warning("payment.recovered: verified success superseded %s for request %s "
                             "(tenant=%s, provider_ref=%s)", prior_status, updated.get("id", ""),
                             tenant, outcome.provider_ref)
            await self._emit(updated, mismatch=mismatch)
        elif effective_status == SUCCEEDED and updated.get("status") != COMPLETED:
            # A verified success that could not land (lost the CAS, or otherwise blocked) must never
            # vanish silently (M4) — alert so ops reconcile by hand; success never loses to failure.
            _log.error("payment.success_dropped: verified success could NOT complete request %s "
                       "(current=%s, tenant=%s, provider_ref=%s)", updated.get("id", ""),
                       updated.get("status"), tenant, outcome.provider_ref)
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
        """Verify a gateway webhook with the tenant's own gateway, then apply it idempotently.

        Cheap junk-id short-circuit (M2): when the body plainly exposes a provider ref this tenant has
        NO request for, skip the outbound verify-by-fetch — an unknown id can never settle anything,
        and this denies an unauthenticated caller a free authenticated call on the shop's real gateway.
        A body that does not expose a resolvable ref falls through to the full verify path unchanged, so
        verification is never weakened."""
        ref = _peek_provider_ref(body)
        stored_currency = ""
        if ref:
            known = await self._req.by_provider_ref(tenant=tenant, provider_ref=ref)
            if known is None:
                return None
            # Server-authoritative currency for a verify-by-fetch gateway (payments-003): the fetch has
            # no currency of its own, so hand it the currency WE recorded for this payment as a fallback
            # for an unusual status-response shape — a real success is no longer refused as unverifiable.
            stored_currency = str(known.get("currency", ""))
        gateway = await self._gw.gateway_for(tenant)
        # OFFLOAD (payments-001): verify_webhook may itself do a blocking verify-by-fetch round trip.
        outcome = await asyncio.to_thread(gateway.verify_webhook, headers=headers, body=body,
                                          stored_currency=stored_currency)
        return await self.apply_outcome(tenant=tenant, outcome=outcome)

    async def reconcile(self, *, tenant: str, request_id: str) -> dict | None:
        """Pull the current status from the gateway for a request whose webhook was missed, and apply
        it through the SAME idempotent seam. Never invents a status the gateway did not report."""
        row = await self._req.get(request_id)
        if row is None or row.get("tenant") != tenant:
            return None
        # Re-query PENDING (missed webhook) AND EXPIRED (H2): our TTL can elapse while the customer
        # pays on a page already open, so a verified success must still be recoverable off an expired
        # request. Any other terminal state is settled — return it unchanged.
        if row.get("status") not in (PENDING, EXPIRED):
            return row
        gateway = await self._gw.gateway_for(tenant)
        outcome = await asyncio.to_thread(                 # OFFLOAD blocking fetch (payments-001)
            gateway.fetch_status, provider_ref=row.get("provider_ref", ""),
            currency=row.get("currency", ""))
        outcome.provider_ref = row.get("provider_ref", "")   # anchor to our stored ref
        return await self.apply_outcome(tenant=tenant, outcome=outcome)
