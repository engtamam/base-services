"""Core billing orchestration — catalog, subscriptions, invoicing, payments, dunning, zatca.

Smart guarantees: charging is idempotent (keyed by invoice id); a gateway failure never
loses the invoice (it enters dunning with escalating backoff → suspend after the cap +
grace); paid invoices are handed to the e-invoicer (zatca) non-fatally; webhooks reconcile
idempotently.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import time
from typing import Any

from app.core import entitlements as _ent
from app.core.entitlement_resolver import EntitlementResolver
from app.core import pricing
from app.core import events as _events
from app.core import settlement as _stl
from app.core.models import (Coupon, Invoice, Payment, Plan, Settlement, Subscription, iso,
                             new_id, now_ts)
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore,
                             UsageStore)
from app.ports.payment_gateway import GatewayUnavailable

_INTERVAL = {"month": 30 * 86400, "year": 365 * 86400, "once": 0}

# Statuses the billing cycle refuses to invoice. A DENYLIST is wrong here and an allowlist is
# right: a status added later (suspended, delinquent, closed) must default to NOT being charged.
# Listing what may be billed means the unknown case declines money; listing what may not means
# it takes money, and only one of those is recoverable.
_BILLABLE = ("active", "trialing", "past_due")


class BillingService:
    def __init__(self, *, plans: PlanStore, coupons: CouponStore, subs: SubscriptionStore,
                 invoices: InvoiceStore, payments: PaymentStore, usage: UsageStore,
                 ledger: LedgerStore, idempo: IdempotencyStore,
                 gateway, tax, einvoicer, pdf, config: dict[str, Any], cache=None,
                 settlements: SettlementStore | None = None, clock=now_ts,
                 emitter=None, grants=None) -> None:
        self._plans, self._coupons, self._subs = plans, coupons, subs
        self._invoices, self._payments, self._usage = invoices, payments, usage
        self._ledger, self._idempo = ledger, idempo
        self._gw, self._tax, self._einv, self._pdf = gateway, tax, einvoicer, pdf
        self._cfg = config
        self._cache = cache
        self._settlements = settlements
        self._clock = clock
        # Publishes domain events so a consumer can react without this service knowing it exists.
        # None means nothing is wired and emitting is a no-op — this service records money
        # correctly with or without anyone listening.
        self._events = emitter
        # The durable path from "they paid" to "they got what they paid for". None means no wallet
        # is wired and nothing is recorded — see core/grants.py for why this is not an event.
        self._grants = grants
        # The union resolver. Config is passed WHOLE and required — see EntitlementResolver's
        # REQUIRED_CFG: a missing key fails at construction rather than silently resolving a
        # tenant against a default nobody chose.
        self._entitlements = EntitlementResolver(subs=subs, plans=plans, cfg=config)

    # ── entitlements (general limit enforcement) ────────────────────────────────
    async def _generation(self, tenant: str) -> str:
        """Tenant generation + global (catalogue) generation. Either bump invalidates."""
        if self._cache is None:
            return "0.0"
        try:
            t = await self._cache.get(f"billinggen:{tenant}")
            g = await self._cache.get("billinggen:global")
            return f"{t or 0}.{g or 0}"
        except Exception:      # noqa: BLE001 — a cache outage degrades to uncached, never to wrong
            return "0.0"

    async def _active_limits(self, tenant: str) -> dict[str, Any]:
        """The tenant's effective limits, billing anchor and subscription id — cached by generation.

        Uncached this scans and sorts the whole subscriptions collection on EVERY
        entitlement check — and feature-flag-service calls it per flag evaluation. Same
        defect the authz PDP had. The generation is bumped inside SubscriptionStore.save /
        PlanStore.create, so an upgrade is visible on the very next request.
        """
        gen = await self._generation(tenant)
        ckey = f"billimits:{gen}:{tenant}"
        if self._cache is not None:
            try:
                raw = await self._cache.get(ckey)
                if raw:
                    return _json.loads(raw)
            except Exception:  # noqa: BLE001
                pass

        # EVERY billable subscription, merged — not the first row that sorted. A tenant who buys
        # three things is entitled to three things, and the shape below is unchanged so existing
        # callers reading `limits` and `anchor` see exactly what they saw before.
        resolved = await self._entitlements.resolve(tenant, self._clock())

        if self._cache is not None:
            try:
                await self._cache.set(ckey, _json.dumps(resolved),
                                      self._cfg.get("limits_cache_ttl", 300))
            except Exception:  # noqa: BLE001
                pass
        return resolved

    async def limits_resolver(self, tenant: str) -> dict[str, Any]:
        """Injected into UsageService — the single source of a tenant's effective limits."""
        return await self._active_limits(tenant)

    async def check_entitlement(self, *, tenant: str, key: str, current: int = 0,
                                amount: int = 1, default: Any = 0) -> dict[str, Any]:
        """Pure/advisory check against a caller-supplied `current`.

        Kept for internal callers that already hold the number. The HTTP surface no longer
        exposes `current`: a client that states its own usage has no limit at all (CWE-602).
        Route handlers use UsageService, which reads usage server-side.
        """
        resolved = await self._active_limits(tenant)
        return _ent.check(resolved["limits"], key, current, amount=amount, default=default)

    # ── catalog ─────────────────────────────────────────────────────────────────
    async def create_plan(self, *, name: str, prices: list[dict], features=None,
                          limits=None, trial_days=0) -> Plan:
        return await self._plans.create(Plan(id=new_id("plan_"), name=name, prices=prices,
                                             features=features or [], limits=limits or {},
                                             trial_days=trial_days))

    async def update_plan(self, plan_id: str, **changes) -> Plan:
        """Correct a live plan: price, limits, features, name, availability.

        EXISTING SUBSCRIBERS ARE NOT REPRICED. They keep the price and limits snapshotted at
        signup — silently changing what someone already agreed to pay is the failure customers
        escalate rather than report, and it is the reason `GRANDFATHER` defaults to on. A platform
        that genuinely wants the live rate turns it off; a tenant is moved deliberately through
        migrate_terms(). Both are choices an adopter makes, not a behaviour baked in here.
        """
        plan = await self._plans.fetch(plan_id)
        if plan is None:
            raise ValueError("PLAN_NOT_FOUND")
        allowed = ("name", "prices", "features", "limits", "trial_days", "active", "listed")
        for key, value in changes.items():
            if key not in allowed:
                # An unknown field silently ignored is how an operator comes to believe they
                # changed something they did not.
                raise ValueError(f"PLAN_FIELD_NOT_EDITABLE:{key}")
            if value is not None:
                setattr(plan, key, value)
        if not plan.prices:
            # A plan with no price bills zero for ever, and the cycle would keep running.
            raise ValueError("PLAN_NEEDS_A_PRICE")
        # create() is an upsert and bumps the catalogue generation, which is what invalidates the
        # cached limits for every non-grandfathered tenant on the very next request.
        return await self._plans.create(plan)

    async def set_plan_availability(self, plan_id: str, *, active: bool | None = None,
                                    listed: bool | None = None) -> Plan:
        """Retire a plan, or hide it, without touching anyone already on it."""
        changes = {}
        if active is not None:
            changes["active"] = active
        if listed is not None:
            changes["listed"] = listed
        return await self.update_plan(plan_id, **changes)

    async def list_plans(self, *, include_hidden: bool = False) -> list[dict]:
        rows = await self._plans.all(self._cfg["list_limit"])
        if include_hidden:
            return rows          # the operator view: everything, including retired plans
        return [r for r in rows if r.get("listed", True) and r.get("active", True)]

    async def upsert_coupon(self, **kw) -> Coupon:
        return await self._coupons.upsert(Coupon(**kw))

    # ── subscriptions ─────────────────────────────────────────────────────────────
    def _price_for(self, plan: Plan, interval: str) -> dict:
        for p in plan.prices:
            if p.get("interval") == interval:
                return p
        return plan.prices[0] if plan.prices else {"amount_minor": 0, "currency": self._cfg["currency"]}

    def _effective_price(self, sub: Subscription, plan: Plan | None) -> dict:
        """Grandfathered: bill the price captured at signup, not the live catalogue."""
        if self._cfg.get("grandfather", True) and sub.price_snapshot:
            return sub.price_snapshot
        return self._price_for(plan, sub.interval) if plan else {}

    def _snapshot(self, plan: Plan, interval: str) -> tuple[dict, dict]:
        return dict(self._price_for(plan, interval)), dict(plan.limits)

    async def subscribe(self, *, tenant: str, plan_id: str, interval: str = "month", quantity: int = 1,
                        coupon_code: str = "", payment_token: str = "", buyer_name: str = "",
                        buyer_vat: str = "", buyer_cr: str = "") -> Subscription:
        plan = await self._plans.fetch(plan_id)
        if plan is None:
            raise ValueError("PLAN_NOT_FOUND")
        if not plan.active:
            # Enforced HERE, at the only place a subscription is created. The flag existed on the
            # model and nothing read it, so "retired" was a label an operator could set and the
            # service would keep selling the plan regardless.
            raise ValueError("PLAN_NOT_AVAILABLE")
        now = now_ts()
        trial_days = plan.trial_days or self._cfg["trial_days"]
        coupon = await self._coupons.fetch(coupon_code) if coupon_code else None
        if coupon and coupon.type == "trial":
            trial_days += int(coupon.value)
        trial_end = now + trial_days * 86400 if trial_days else 0.0
        price_snap, limits_snap = self._snapshot(plan, interval)   # lock the signup terms
        sub = Subscription(
            id=new_id("sub_"), tenant=tenant, plan_id=plan_id, interval=interval, quantity=quantity,
            coupon_code=coupon_code, payment_token=payment_token, buyer_name=buyer_name,
            buyer_vat=buyer_vat, buyer_cr=buyer_cr,
            price_snapshot=price_snap, limits_snapshot=limits_snap, terms_version=iso(),
            status="trialing" if trial_days else "active",
            current_period_start=now, current_period_end=now + _INTERVAL.get(interval, _INTERVAL["month"]),
            trial_end=trial_end, created=iso())
        return await self._subs.save(sub)

    async def change_plan(self, sub_id: str, *, new_plan_id: str, interval: str | None = None,
                          quantity: int | None = None) -> dict[str, Any]:
        sub = await self._subs.fetch(sub_id)
        if sub is None:
            raise ValueError("SUB_NOT_FOUND")
        old_plan = await self._plans.fetch(sub.plan_id)
        new_plan = await self._plans.fetch(new_plan_id)
        if new_plan is None:
            raise ValueError("PLAN_NOT_FOUND")
        old_price = pricing.line_amount(self._effective_price(sub, old_plan), quantity=sub.quantity)
        new_interval = interval or sub.interval
        new_qty = quantity or sub.quantity
        new_price = pricing.line_amount(self._price_for(new_plan, new_interval), quantity=new_qty)
        delta = pricing.proration_delta(old_price, new_price, period_start=sub.current_period_start,
                                        period_end=sub.current_period_end, now=now_ts())
        sub.plan_id, sub.interval, sub.quantity = new_plan_id, new_interval, new_qty
        # a deliberate plan change re-captures the terms (the customer chose them)
        sub.price_snapshot, sub.limits_snapshot = self._snapshot(new_plan, new_interval)
        sub.terms_version = iso()
        await self._subs.save(sub)
        return {"subscription_id": sub.id, "proration_minor": delta}

    async def migrate_terms(self, sub_id: str) -> dict[str, Any]:
        """Explicitly move a grandfathered subscription onto the CURRENT plan terms.

        Changing an existing customer's price/limits is always an explicit act — never a
        side-effect of editing the plan catalogue.
        """
        sub = await self._subs.fetch(sub_id)
        if sub is None:
            raise ValueError("SUB_NOT_FOUND")
        plan = await self._plans.fetch(sub.plan_id)
        if plan is None:
            raise ValueError("PLAN_NOT_FOUND")
        old = dict(sub.price_snapshot)
        sub.price_snapshot, sub.limits_snapshot = self._snapshot(plan, sub.interval)
        sub.terms_version = iso()
        await self._subs.save(sub)
        return {"subscription_id": sub.id, "migrated": True,
                "old_amount_minor": old.get("amount_minor"),
                "new_amount_minor": sub.price_snapshot.get("amount_minor"),
                "limits": sub.limits_snapshot}

    async def pause(self, sub_id: str) -> Subscription | None:
        """Active → paused. Refuses anything else.

        Both of these used to set the status unconditionally, which was survivable while nothing
        called them and is not once there is a route. Pausing a CANCELED subscription would move
        it out of the terminal state, and the pair below is worse.
        """
        return await self._set_status(sub_id, "paused", allowed_from=("active",))

    async def reactivate(self, sub_id: str) -> Subscription | None:
        """Paused → active. Only from paused, and that restriction is the point.

        Reactivating a CANCELED subscription would resume charging someone who left — the state
        machine, not the route, has to refuse it, because a caller passing an id has no way to
        know it is terminal. `cancel_at_period_end` is cleared: a customer who paused and came
        back has withdrawn the intent to leave, and leaving the flag set would end the
        subscription at the boundary anyway, silently undoing the reactivation they just asked for.
        """
        sub = await self._set_status(sub_id, "active", allowed_from=("paused",))
        if sub is not None and sub.cancel_at_period_end:
            sub.cancel_at_period_end = False
            await self._subs.save(sub)
        return sub

    async def cancel(self, sub_id: str, *, at_period_end: bool = True) -> Subscription | None:
        sub = await self._subs.fetch(sub_id)
        if sub is None:
            return None
        if at_period_end:
            sub.cancel_at_period_end = True
        else:
            sub.status = "canceled"
        saved = await self._subs.save(sub)
        if self._events is not None:
            # `at_period_end` is carried because the two cases are genuinely different to a
            # consumer: one keeps serving the customer until the period ends, the other stops now.
            # A consumer that cannot tell them apart either cuts access early or never cuts it.
            await self._events.emit(_events.SUBSCRIPTION_CANCELLED, {
                "tenant": sub.tenant, "subscription_id": sub.id, "plan_id": sub.plan_id,
                "at_period_end": bool(at_period_end), "status": sub.status,
                "current_period_end": getattr(sub, "current_period_end", "") or "",
            })
        return saved

    async def _set_status(self, sub_id: str, status: str,
                          *, allowed_from: tuple[str, ...] = ()) -> Subscription | None:
        """Returns None when absent; raises ValueError on an illegal transition.

        The two are deliberately different: absent is a 404, but an illegal transition is a 409 —
        the subscription exists and the caller may not do that to it. Collapsing them into None
        would report 'no such subscription' for one that is merely canceled, and the caller would
        reasonably retry a create.
        """
        sub = await self._subs.fetch(sub_id)
        if sub is None:
            return None
        if sub.status == status:
            # Before the transition check, not after: already-in-the-target-state is a retry, and
            # the target is never in allowed_from, so testing legality first would 409 every
            # duplicate click and every redelivered webhook.
            return sub
        if allowed_from and sub.status not in allowed_from:
            raise ValueError(f"cannot move a {sub.status!r} subscription to {status!r}")
        sub.status = status
        return await self._subs.save(sub)

    async def record_usage(self, *, tenant: str, subscription_id: str, metric: str, quantity: int) -> dict:
        return await self._usage.record(tenant, subscription_id, metric, quantity)

    # ── billing cycle ─────────────────────────────────────────────────────────────
    async def run_cycle(self, sub_id: str) -> Invoice:
        sub = await self._subs.fetch(sub_id)
        if sub is None:
            raise ValueError("SUB_NOT_FOUND")
        if sub.status not in _BILLABLE:
            # This check was absent, and it made two features decorative: the cycle invoiced
            # PAUSED and CANCELED subscriptions exactly like active ones. Pausing suppressed
            # nothing, and cancelling only stopped renewals if some caller happened not to run
            # the cycle — charging people who had left, which is the failure customers escalate
            # rather than report. The state has to be enforced where the money is made.
            raise ValueError(f"SUB_NOT_BILLABLE:{sub.status}")
        key = f"cycle:{sub.id}:{int(sub.current_period_end)}"     # deterministic → no double-charge
        prior = await self._idempo.seen(key)
        if prior:
            return await self._invoices.fetch(prior["result"]["invoice_id"]) or Invoice(id="", tenant=sub.tenant)
        plan = await self._plans.fetch(sub.plan_id)
        price = self._effective_price(sub, plan)          # grandfathered signup price
        currency = price.get("currency", self._cfg["currency"])
        usage_units = 0
        if price.get("usage_metric"):
            usage_units = await self._usage.aggregate(sub.id, price["usage_metric"],
                                                      sub.current_period_start, sub.current_period_end)
        subtotal = pricing.line_amount(price, quantity=sub.quantity, usage_units=usage_units)
        coupon = await self._coupons.fetch(sub.coupon_code) if sub.coupon_code else None
        discount = pricing.coupon_discount(coupon.to_dict() if coupon else None, subtotal)
        taxable, tax, total = self._split_tax(subtotal - discount, price.get("includes_tax", False),
                                              reverse_charge=bool(sub.buyer_vat) and self._cfg.get("reverse_charge", False))
        lines = [{"description": (plan.name if plan else "Subscription"),
                  "quantity": sub.quantity, "unit_minor": price.get("amount_minor", 0),
                  "amount_minor": taxable}]
        # Captured from the plan AS SOLD, onto the invoice. Recomputing this later from the live
        # plan would mean a repriced plan silently changes what an old invoice granted.
        granted = int((plan.limits or {}).get(self._cfg["plan_credit_limit_key"], 0) or 0) if plan else 0
        inv = Invoice(id=new_id("inv_"), tenant=sub.tenant, number=await self._invoices.next_number(sub.tenant),
                      subscription_id=sub.id, lines=lines, subtotal_minor=taxable, discount_minor=discount,
                      tax_minor=tax, total_minor=total, currency=currency, status="open", created=iso(),
                      # period_end travels WITH the invoice so a `reset` grant expires at the end
                      # of the period it was sold for, even if delivery is retried days later.
                      meta={"grant_minor": granted,
                            "period_end": sub.current_period_end} if granted > 0 else {})
        await self._invoices.save(inv)
        sub.current_period_start = sub.current_period_end
        sub.current_period_end += _INTERVAL.get(sub.interval, _INTERVAL["month"])
        if sub.status == "trialing":
            sub.status = "active"
        await self._subs.save(sub)
        await self._charge_invoice(inv, sub.payment_token)
        await self._idempo.mark(key, {"invoice_id": inv.id})
        if self._events is not None:
            # Emitted for the RENEWAL, whether or not the charge succeeded — the period rolled
            # either way, and a consumer granting a plan's included allowance needs the period, not
            # the payment. Whether the money arrived is payment.succeeded / payment.failed, which a
            # consumer that cares about payment subscribes to separately.
            #
            # `period_key` is the idempotency key the cycle itself used, so a consumer granting
            # credits can key on the same period and cannot double-grant across a replay.
            await self._events.emit(_events.SUBSCRIPTION_RENEWED, {
                "tenant": sub.tenant, "subscription_id": sub.id, "plan_id": sub.plan_id,
                "invoice_id": inv.id, "period_key": key,
                "period_start": sub.current_period_start, "period_end": sub.current_period_end,
                "limits": dict(sub.limits_snapshot or {}) if hasattr(sub, "limits_snapshot") else {},
            })
        return await self._invoices.fetch(inv.id) or inv

    def _split_tax(self, base_minor: int, includes_tax: bool, *, reverse_charge: bool = False) -> tuple[int, int, int]:
        """Return (taxable_net, tax, total). includes_tax → split VAT out of a gross price."""
        if reverse_charge:                                   # B2B cross-border: buyer accounts for VAT
            return base_minor, 0, base_minor
        if includes_tax:
            from decimal import ROUND_HALF_UP, Decimal
            rate = Decimal(str(self._cfg.get("vat_rate", "0.15")))
            net = int((Decimal(base_minor) / (1 + rate)).quantize(Decimal(1), ROUND_HALF_UP))
            return net, base_minor - net, base_minor
        tax = self._tax.compute(amount_minor=base_minor)
        return base_minor, tax, base_minor + tax

    async def run_overage(self, sub_id: str) -> Invoice | None:
        """Bill metered usage over the plan's included units (idempotent per period). Zero → none."""
        sub = await self._subs.fetch(sub_id)
        if sub is None:
            raise ValueError("SUB_NOT_FOUND")
        plan = await self._plans.fetch(sub.plan_id)
        price = self._effective_price(sub, plan)
        metric = price.get("usage_metric")
        if not metric or int(price.get("overage_minor", 0)) <= 0:
            return None
        period = time.strftime("%Y-%m", time.gmtime(sub.current_period_start))
        key = f"overage:{sub.id}:{period}"
        if await self._idempo.seen(key):
            return None
        units = await self._usage.aggregate(sub.id, metric, sub.current_period_start, sub.current_period_end)
        over = max(0, units - int(price.get("included_units", 0)))
        if over <= 0:
            await self._idempo.mark(key, {"overage": 0})
            return None
        net = over * int(price["overage_minor"])
        taxable, tax, total = self._split_tax(net, False)     # overage = net + VAT on top
        inv = Invoice(id=new_id("inv_"), tenant=sub.tenant, number=await self._invoices.next_number(sub.tenant),
                      subscription_id=sub.id,
                      lines=[{"description": f"Overage: {over} × {metric}", "quantity": over,
                              "unit_minor": price["overage_minor"], "amount_minor": net}],
                      subtotal_minor=net, tax_minor=tax, total_minor=total,
                      currency=price.get("currency", self._cfg["currency"]), status="open", created=iso())
        await self._invoices.save(inv)
        await self._charge_invoice(inv, sub.payment_token)
        await self._idempo.mark(key, {"invoice_id": inv.id, "overage": over})
        return await self._invoices.fetch(inv.id) or inv

    async def _charge_invoice(self, inv: Invoice, token: str) -> None:
        pay = Payment(id=new_id("pay_"), tenant=inv.tenant, invoice_id=inv.id,
                      amount_minor=inv.total_minor, currency=inv.currency, created=iso())
        if inv.total_minor <= 0:                            # nothing to charge (100% coupon / trial)
            pay.status = "succeeded"
            await self._payments.save(pay)
            await self._mark_paid(inv)
            return
        try:
            charge = self._gw.charge(amount_minor=inv.total_minor, currency=inv.currency,
                                     token=token, idempotency_key=inv.id, description=inv.number)
        except GatewayUnavailable as ex:
            pay.status = "failed"
            pay.error = str(ex)
            await self._dunning(pay)
            await self._payments.save(pay)
            return
        pay.provider, pay.provider_id = charge.provider, charge.provider_id
        if charge.ok:
            pay.status = "succeeded"
            await self._payments.save(pay)
            await self._mark_paid(inv)
        else:
            pay.status = "failed"
            pay.error = charge.error
            await self._dunning(pay)
            await self._payments.save(pay)

    async def _mark_paid(self, inv: Invoice, *, paid_at: str = "", source: str = "") -> None:
        """The ONE place an invoice becomes paid.

        A card charge, a webhook reconcile and an off-gateway settlement all land here, so
        entitlements, ZATCA e-invoicing and dunning behave identically for every one of them.
        That is the whole point: one truth about money.

        `paid_at` defaults to now for a gateway charge (the money moved as we watched). A
        settlement passes the bank's VALUE DATE — stamping `now()` would file the tax invoice
        in the wrong period and recognise revenue in the wrong month.
        """
        inv.status = "paid"
        inv.paid_at = paid_at or iso()
        if not inv.settled_minor:
            inv.settled_minor = inv.total_minor
        buyer = await self._buyer_for(inv)
        inv.einvoice = self._einv.finalize(tenant=inv.tenant, invoice=self._einvoice_payload(inv, buyer))
        await self._invoices.save(inv)
        await self._ledger.record(tenant=inv.tenant, kind="charge", amount_minor=inv.total_minor,
                                  currency=inv.currency, invoice_id=inv.id,
                                  source=source or inv.number)
        # Emitted from HERE, the one place an invoice becomes paid, rather than from each of the
        # three callers. A card charge, a webhook reconcile and a settlement must produce the same
        # event or a consumer's view of "paid" depends on how the money arrived.
        #
        # AFTER the ledger, never before: the event says the payment is recorded, and a consumer
        # that reads back on receipt must not find a row that is not there yet.
        # The grant is written down BEFORE the event, and both come after the ledger. The order is
        # deliberate: the durable promise must exist before anything announces the payment, or a
        # consumer can act on a credit that nothing is committed to delivering.
        if self._grants is not None:
            grantable = int((inv.meta or {}).get("grant_minor", 0) or 0)
            if grantable > 0:
                # `reset` means a plan's monthly allowance does not roll over, expressed as an
                # EXPIRY on the grant rather than by zeroing the balance first. Zeroing would also
                # destroy credits the customer bought separately — which they paid for and which
                # have nothing to do with the plan's allowance.
                expires = None
                if self._cfg["plan_credit_grant_mode"] == "reset":
                    expires = float((inv.meta or {}).get("period_end", 0) or 0) or None
                await self._grants.record(
                    tenant=inv.tenant, invoice_id=inv.id, fund=self._cfg["grant_fund"],
                    amount_minor=grantable, reason=f"invoice:{inv.number}", expires_at=expires)
        if self._events is not None:
            payload = _events.payment_payload(inv, source=source or inv.number)
            await self._events.emit(_events.INVOICE_PAID, payload)
            await self._events.emit(_events.PAYMENT_SUCCEEDED, payload)

    # ── off-gateway settlement (Phase 27) ───────────────────────────────────────
    async def settle(self, *, invoice_id: str, method: str, reference: str, amount_minor: int,
                     paid_at: float, issuer: str = "", currency: str = "", actor: str = "",
                     proof_file_id: str = "", note: str = "",
                     tenant: str | None = None) -> dict[str, Any]:
        """Settle an invoice from a payment that never touched a gateway.

        Concurrency, not caching, is what this needs. Two admins settling the same invoice, or
        two tranches of one wire landing together, must not both finalize the e-invoice. The
        payment identity is claimed ATOMICALLY first, and `settled_minor` accumulates through
        the same bounded increment that stops a quota being oversold.
        """
        inv = await self._invoices.fetch(invoice_id)
        if inv is None or (tenant is not None and inv.tenant != tenant):
            raise _stl.SettlementError("INVOICE_NOT_FOUND")   # never confirm existence

        method, issuer, reference = _stl.normalize(
            tenant=inv.tenant, method=method, issuer=issuer, reference=reference,
            methods=self._cfg["settlement_methods"])

        # A RETRY is answered BEFORE any state check. The caller may not know whether their
        # first request landed, and by then the invoice is `paid` — validating first would
        # reject the very retry we promised to honour.
        key = _stl.identity(inv.tenant, method, issuer, reference)
        replay = await self._replay_settlement(key, inv)
        if replay is not None:
            return replay

        _stl.check_status(inv.status)

        paid_ts = _stl.check_paid_at(
            paid_at, now=now_ts(), invoice_created=self._created_ts(inv),
            max_backdate_days=self._cfg["settlement_max_backdate_days"])

        plan = _stl.check_amount(
            amount_minor=amount_minor, currency=currency or inv.currency,
            invoice_currency=inv.currency, invoice_total_minor=inv.total_minor,
            already_settled_minor=inv.settled_minor,
            allow_partial=self._cfg["settlement_allow_partial"],
            tolerance_minor=self._cfg["settlement_tolerance_minor"])

        # 1. Claim the real-world payment, atomically, before anything else is written.
        #    A GENERIC error: naming the owner would turn this endpoint into an oracle for
        #    other tenants' transfers.
        if not await self._settlements.claim_identity(key):
            # Lost the race to an in-flight duplicate (its index row may not exist yet).
            replay = await self._replay_settlement(key, inv)
            if replay is not None:
                return replay
            raise _stl.SettlementError("SETTLEMENT_ALREADY_RECORDED")

        # 2. Apply the money, bounded by what is outstanding. Two racing settlements cannot
        #    both take the last halala: `incr_if_below` decides, not this process.
        applied, settled_after = await self._invoices_settled_incr(
            inv.id, plan["applied"], inv.total_minor)
        if not applied:
            await self._settlements.release_identity(key)     # nothing was written
            raise _stl.SettlementError("AMOUNT_EXCEEDS_OUTSTANDING")

        stl = Settlement(id=new_id("stl_"), tenant=inv.tenant, invoice_id=inv.id, method=method,
                         issuer=issuer, reference=reference, amount_minor=plan["applied"],
                         currency=inv.currency, paid_at=_stl.iso_of(paid_ts),
                         proof_file_id=proof_file_id, actor=actor, note=note,
                         status=_stl.CONFIRMED, created=iso())
        await self._settlements.save(stl)
        await self._settlements.index_identity(key, stl.id, inv.id)

        # 3. The COUNTER is the truth. The invoice row is not written for a partial payment.
        #
        #    Writing `inv.settled_minor = settled_after` here would be a lost update: three
        #    concurrent tranches each read-modify-write the same row and the last writer wins
        #    with a stale value — and a late partial writer could overwrite `paid` back to
        #    `partially_paid`. Postgres exposed exactly that; the in-memory adapter did not.
        #    So the only invoice write during settlement is the single `paid` transition below.
        remaining = inv.total_minor - settled_after
        closes = remaining <= max(0, self._cfg["settlement_tolerance_minor"])

        einvoice_finalized = bool(inv.einvoice)
        if closes:
            # Several settlements can observe `closes` (a tolerance lets a tranche close the
            # invoice while another is still in flight). Exactly ONE may finalize the e-invoice
            # and write the ledger row: the same 1-of-1 claim that guards the payment identity.
            if await self._settlements.claim_identity(f"closed:{inv.id}"):
                inv = await self._invoices.fetch(inv.id)
                inv.settled_minor = settled_after
                await self._mark_paid(inv, paid_at=stl.paid_at, source=f"{method}:{reference}")
                einvoice_finalized = True
                # Re-activate the entitlement, exactly as the gateway webhook does when a card
                # payment lands (see the reconcile path). An enterprise invoice that lapsed to
                # `past_due` and is then paid by bank transfer must turn the subscription back on —
                # otherwise the invoice reads `paid` while the entitlement stays off, which is the
                # invoice-and-entitlement-disagree split an off-gateway payment is meant to close.
                if inv.subscription_id:
                    sub = await self._subs.fetch(inv.subscription_id)
                    if sub and sub.status == "past_due":
                        sub.status = "active"
                        await self._subs.save(sub)

        status = await self.invoice_state(inv.id)
        return {"settlement": stl.to_dict(), "invoice_status": status,
                "settled_minor": settled_after, "remaining_minor": max(0, remaining),
                "einvoice_finalized": einvoice_finalized}

    async def _replay_settlement(self, key: str, inv: Invoice) -> dict[str, Any] | None:
        """A retry of the SAME transfer against the SAME invoice returns the original result.

        Against a DIFFERENT invoice it is a payment that never happened — the caller falls
        through to a GENERIC refusal, so the response cannot be used to probe another tenant's
        transfers. A REVERSED settlement is never replayed: that transfer was recalled, and
        resurrecting it would re-settle an invoice with money the bank took back.
        """
        prior = await self._settlements.resolve_identity(key)
        if not prior or prior.get("invoice_id") != inv.id:
            return None
        existing = await self._settlements.fetch(prior["settlement_id"])
        if existing is None or existing.status == _stl.REVERSED:
            return None
        settled = await self.settled_minor(inv.id)
        fresh = await self._invoices.fetch(inv.id)
        return {"settlement": existing.to_dict(), "idempotent": True,
                "invoice_status": await self.invoice_state(inv.id),
                "settled_minor": settled,
                "remaining_minor": max(0, inv.total_minor - settled),
                "einvoice_finalized": bool(fresh.einvoice if fresh else False)}

    async def settled_minor(self, invoice_id: str) -> int:
        """The authoritative amount settled: the atomic counter, never the invoice's copy."""
        row = await self._invoices._store.get("invoice_settled", invoice_id)  # noqa: SLF001
        return int((row or {}).get("n", 0))

    async def invoice_state(self, invoice_id: str) -> str:
        """`partially_paid` is DERIVED from the counter, so it can never be a lost update."""
        inv = await self._invoices.fetch(invoice_id)
        if inv is None:
            return "unknown"
        if inv.status == "paid":
            return "paid"
        settled = await self.settled_minor(invoice_id)
        if settled > 0 and inv.status == "open":
            return "partially_paid"          # dunning still chases the remainder
        return inv.status

    async def _invoices_settled_incr(self, invoice_id: str, delta: int,
                                     total_minor: int) -> tuple[bool, int]:
        """Atomic `settled_minor` accumulation, bounded by the invoice total."""
        return await self._invoices._store.incr_if_below(     # noqa: SLF001 — same module family
            "invoice_settled", invoice_id, int(delta), int(total_minor))

    async def reverse_settlement(self, *, settlement_id: str, reason: str = "",
                                 actor: str = "", tenant: str | None = None) -> dict[str, Any]:
        """A recalled or bounced transfer.

        History is never deleted. The settlement is marked `reversed`, the money comes back
        off `settled_minor`, and if the invoice had been closed we issue a CREDIT NOTE exactly
        as a card refund does — a cleared ZATCA invoice cannot be un-cleared.
        """
        stl = await self._settlements.fetch(settlement_id)
        if stl is None or (tenant is not None and stl.tenant != tenant):
            raise _stl.SettlementError("SETTLEMENT_NOT_FOUND")
        if stl.status == _stl.REVERSED:
            return {"settlement": stl.to_dict(), "idempotent": True}

        inv = await self._invoices.fetch(stl.invoice_id)
        # A release always applies and clamps at 0 (see incr_if_below): giving money back is
        # never refused.
        _, settled_after = await self._invoices_settled_incr(
            stl.invoice_id, -stl.amount_minor, inv.total_minor if inv else 0)

        stl.status = _stl.REVERSED
        stl.reversed_reason = reason
        await self._settlements.save(stl)
        # The identity stays claimed: the transfer really did happen once, and a reversal must
        # not free the reference for reuse. The CLOSE claim, however, is released — the invoice
        # is open again and a later settlement must be able to close it.
        await self._settlements.release_identity(f"closed:{stl.invoice_id}")

        credit_note = None
        if inv is not None:
            was_paid = inv.status == "paid"
            inv.settled_minor = settled_after
            inv.status = "open"          # `partially_paid` is DERIVED from the counter
            inv.paid_at = ""
            await self._invoices.save(inv)
            if was_paid:
                await self._ledger.record(tenant=inv.tenant, kind="refund",
                                          amount_minor=-stl.amount_minor, currency=inv.currency,
                                          invoice_id=inv.id, source=f"reversal:{stl.id}")
                credit_note = {"invoice_id": inv.id, "amount_minor": stl.amount_minor}

        state = await self.invoice_state(stl.invoice_id) if inv else "unknown"
        return {"settlement": stl.to_dict(), "invoice_status": state,
                "settled_minor": settled_after, "credit_note": credit_note, "actor": actor}

    async def settlements_for(self, invoice_id: str, tenant: str | None = None) -> list[dict]:
        inv = await self._invoices.fetch(invoice_id)
        if inv is None or (tenant is not None and inv.tenant != tenant):
            raise _stl.SettlementError("INVOICE_NOT_FOUND")
        return await self._settlements.for_invoice(invoice_id, self._cfg["list_limit"])

    @staticmethod
    def _created_ts(inv: Invoice) -> float:
        try:
            return _dt.datetime.fromisoformat(inv.created).timestamp() if inv.created else 0.0
        except ValueError:
            return 0.0

    async def _buyer_for(self, inv: Invoice) -> dict[str, Any]:
        if inv.subscription_id:
            sub = await self._subs.fetch(inv.subscription_id)
            if sub:
                return {"name": sub.buyer_name, "vat_number": sub.buyer_vat, "id": inv.tenant}
        return {"id": inv.tenant}

    def _einvoice_payload(self, inv: Invoice, buyer: dict[str, Any]) -> dict[str, Any]:
        return {"number": inv.number, "issued_at": int(now_ts()), "currency": inv.currency,
                "vat_rate": self._cfg.get("vat_rate", "0.15"), "seller": {}, "buyer": buyer,
                "lines": [{"description": li["description"], "qty": li.get("quantity", 1),
                           "amount_minor": li["amount_minor"]} for li in inv.lines]}

    # ── dunning ────────────────────────────────────────────────────────────────────
    async def _dunning(self, pay: Payment) -> None:
        base_h, cap = self._cfg["dunning_base_hours"], self._cfg["dunning_max_attempts"]
        if pay.attempts >= cap:
            pay.next_retry_at = None                        # give up → suspend
            await self._suspend(pay.tenant, pay.invoice_id)
        else:
            pay.next_retry_at = now_ts() + base_h * (2 ** pay.attempts) * 3600
        pay.attempts += 1
        if self._events is not None:
            # `final` distinguishes "we will try again" from "we have given up and suspended".
            # A consumer that treats every failure as final dunning-mails a customer whose retry
            # would have succeeded an hour later.
            await self._events.emit(_events.PAYMENT_FAILED, {
                "tenant": pay.tenant, "invoice_id": pay.invoice_id, "payment_id": pay.id,
                "attempts": pay.attempts, "final": pay.next_retry_at is None,
                "next_retry_at": pay.next_retry_at or "",
            })

    async def _suspend(self, tenant: str, invoice_id: str) -> None:
        inv = await self._invoices.fetch(invoice_id)
        if inv and inv.subscription_id:
            sub = await self._subs.fetch(inv.subscription_id)
            if sub:
                sub.status = "past_due"
                await self._subs.save(sub)

    async def dunning_sweep(self) -> dict[str, Any]:
        due = await self._payments.due(now_ts(), self._cfg["list_limit"])
        retried = 0
        for pay in due:
            inv = await self._invoices.fetch(pay.invoice_id)
            if inv is None or inv.status == "paid":
                continue
            sub = await self._subs.fetch(inv.subscription_id)
            token = sub.payment_token if sub else ""
            try:
                charge = self._gw.charge(amount_minor=inv.total_minor, currency=inv.currency,
                                         token=token, idempotency_key=inv.id, description=inv.number)
            except GatewayUnavailable:
                await self._dunning(pay)
                await self._payments.save(pay)
                continue
            if charge.ok:
                pay.status, pay.provider, pay.provider_id = "succeeded", charge.provider, charge.provider_id
                pay.next_retry_at = None
                await self._payments.save(pay)
                await self._mark_paid(inv)
                if sub and sub.status == "past_due":
                    sub.status = "active"
                    await self._subs.save(sub)
            else:
                pay.error = charge.error
                await self._dunning(pay)
                await self._payments.save(pay)
            retried += 1
        return {"retried": retried, "due": len(due)}

    # ── webhooks ───────────────────────────────────────────────────────────────────
    async def handle_webhook(self, provider: str, headers: dict, body: bytes,
                             secret: str | tuple[str, ...]) -> dict[str, Any]:
        """Reconcile a gateway callback. `secret` may be SEVERAL candidate signing secrets.

        During a rotation both the new and the previous secret must verify. The gateway signed
        whatever it held when it sent, and it may still be sending with the old one — for a
        callback that is already in flight there is no version to negotiate. Accepting only the
        current secret would reject those as forged, and a rejected payment.succeeded means an
        invoice stays unpaid while the customer's money has already moved.
        """
        candidates = (secret,) if isinstance(secret, str) else tuple(secret)
        event = None
        for candidate in candidates or ("",):
            event = self._gw.verify_webhook(headers=headers, body=body, secret=candidate)
            if event.verified:
                break
        if event is None or not event.verified:
            return {"ok": False, "reason": "SIGNATURE_INVALID",
                    "type": event.type if event else ""}
        pay = await self._payments.by_provider_id(event.provider_id) if event.provider_id else None
        if pay is None:
            return {"ok": True, "reconciled": False, "type": event.type}
        inv = await self._invoices.fetch(pay.invoice_id)
        if event.type == "payment.succeeded" and pay.status != "succeeded":
            pay.status = "succeeded"
            pay.next_retry_at = None
            await self._payments.save(pay)
            if inv and inv.status != "paid":
                await self._mark_paid(inv)
        elif event.type == "payment.failed" and pay.status != "failed":
            pay.status = "failed"
            await self._dunning(pay)
            await self._payments.save(pay)
        elif event.type == "refund" and pay.status != "refunded":
            pay.status = "refunded"
            await self._payments.save(pay)
            if inv:
                inv.status = "void"
                await self._invoices.save(inv)
        return {"ok": True, "reconciled": True, "type": event.type, "payment_status": pay.status}

    async def refund(self, payment_id: str, amount_minor: int | None = None) -> dict[str, Any]:
        pay = await self._payments.fetch(payment_id)
        if pay is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        charge = self._gw.refund(provider_id=pay.provider_id, amount_minor=amount_minor)
        if charge.ok:
            pay.status = "refunded"
            await self._payments.save(pay)
            inv = await self._invoices.fetch(pay.invoice_id)
            if inv:
                inv.status = "void"
                inv.credit_note = True
                await self._invoices.save(inv)
                await self._ledger.record(tenant=inv.tenant, kind="refund",
                                          amount_minor=-(amount_minor or inv.total_minor),
                                          currency=inv.currency, invoice_id=inv.id, source="refund")
                if self._events is not None:
                    # A consumer that granted something on payment.succeeded needs to know to take
                    # it back. Without this, a refunded customer keeps whatever the payment bought.
                    await self._events.emit(_events.PAYMENT_REFUNDED, {
                        **_events.payment_payload(inv, payment_id=pay.id, source="refund"),
                        "amount_minor": amount_minor or inv.total_minor,
                    })
        return {"ok": charge.ok, "status": charge.status}

    def render_pdf(self, invoice: dict) -> tuple[str, bytes] | None:
        return self._pdf.render(invoice)
