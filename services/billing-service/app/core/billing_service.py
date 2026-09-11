"""Core billing orchestration — catalog, subscriptions, invoicing, payments, dunning, zatca.

Smart guarantees: charging is idempotent (keyed by invoice id); a gateway failure never
loses the invoice (it enters dunning with escalating backoff → suspend after the cap +
grace); paid invoices are handed to the e-invoicer (zatca) non-fatally; webhooks reconcile
idempotently.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import secrets as _secrets
import time
from decimal import ROUND_HALF_UP as _ROUND_HALF_UP
from decimal import Decimal as _Decimal
from typing import Any

from app.core import coupons as _coupons
from app.core import entitlements as _ent
from app.core.entitlement_resolver import EntitlementResolver
from app.core import periods
from app.core import pricing
from app.core import events as _events
from app.core import settlement as _stl
from app.core import tax_profile
from app.core.models import (Coupon, DiscountRule, Invoice, Payment, Plan, Settlement, Subscription,
                             iso, new_id, now_ts)
from app.core.stores import (CouponStore, DiscountRuleStore, IdempotencyStore, InvoiceStore,
                             LedgerStore, PaymentStore, PlanStore, SettlementStore,
                             SubscriptionStore, UsageStore)
from app.observability.logging import get_logger
from app.ports.payment_gateway import GatewayUnavailable
from app.ports.wallet import WalletRefused, WalletUnavailable

_log = get_logger("app.billing")

_INTERVAL = {"month": 30 * 86400, "year": 365 * 86400, "once": 0}


def _epoch(iso_str: str) -> float:
    """An ISO-8601 timestamp (as produced by models.iso()) to epoch seconds; 0.0 if unparseable, so
    an age filter fails OPEN (includes the row) rather than hiding a pending payment on a bad string."""
    if not iso_str:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0

# Statuses the billing cycle refuses to invoice. A DENYLIST is wrong here and an allowlist is
# right: a status added later (suspended, delinquent, closed) must default to NOT being charged.
# Listing what may be billed means the unknown case declines money; listing what may not means
# it takes money, and only one of those is recoverable.
_BILLABLE = ("active", "trialing", "past_due")


class UpgradeChargeFailed(ValueError):
    """The upgrade's prorated charge was DECLINED (or no payment method) — the plan did not change.

    A synchronous refusal the caller surfaces as a 402: the shop can fix the card (update-payment-method)
    and retry. Deliberately NOT the async dunning path — an upgrade a customer just asked for either
    happens now or is refused now, never queued to retry silently.
    """


class UpgradeChargeUnavailable(RuntimeError):
    """The gateway was unreachable while charging the upgrade — try again. Surfaced as a 503, distinct
    from a decline: retrying a decline is pointless, retrying an outage is exactly right."""


class BillingService:
    def __init__(self, *, plans: PlanStore, coupons: CouponStore, subs: SubscriptionStore,
                 invoices: InvoiceStore, payments: PaymentStore, usage: UsageStore,
                 ledger: LedgerStore, idempo: IdempotencyStore,
                 gateway, tax, einvoicer, pdf, config: dict[str, Any], cache=None,
                 settlements: SettlementStore | None = None, clock=now_ts,
                 emitter=None, grants=None, gateway_resolver=None, wallet=None,
                 paylinks: "PayLinkStore | None" = None,
                 discount_rules: "DiscountRuleStore | None" = None) -> None:
        self._plans, self._coupons, self._subs = plans, coupons, subs
        # Cross-app, ownership-triggered discount rules (sibling to coupons). None ⇒ the feature is
        # simply off: no rule ever applies, and renewals price exactly as before.
        self._rules = discount_rules
        self._invoices, self._payments, self._usage = invoices, payments, usage
        self._paylinks = paylinks
        self._ledger, self._idempo = ledger, idempo
        self._gw, self._tax, self._einv, self._pdf = gateway, tax, einvoicer, pdf
        # Resolves a gateway by (tenant, driver) for PINNED mode — async, injected so core imports no
        # adapter/container. None ⇒ pinning falls back to the active gateway (i.e. behaves as `active`).
        self._gw_resolver = gateway_resolver
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
        # The wallet for RENEWAL-BY-DEBIT (distinct from the grant outbox's use of it): a
        # subscription pinned to payment_gateway=="wallet" renews by debiting a configured fund
        # instead of charging a card. None ⇒ no wallet wired ⇒ the feature is simply unavailable and
        # a wallet-pinned sub fails its charge loudly rather than silently taking free service.
        self._wallet = wallet
        # The union resolver. Config is passed WHOLE and required — see EntitlementResolver's
        # REQUIRED_CFG: a missing key fails at construction rather than silently resolving a
        # tenant against a default nobody chose.
        self._entitlements = EntitlementResolver(subs=subs, plans=plans, cfg=config)

    # ── gateway selection (renewal mode: active | pinned) ───────────────────────
    def _pinned_mode(self) -> bool:
        return self._cfg.get("renewal_gateway_mode") == "pinned" and self._gw_resolver is not None

    async def _gw_for_sub(self, sub) -> Any:
        """The gateway a subscription's charges route to.

        `active` mode → the currently-active gateway (today's behaviour). `pinned` mode → the sub's
        OWN gateway (Subscription.payment_gateway). A pinned gateway that will not build comes back as
        a fail-closed _UnconfiguredGateway whose charge raises GatewayUnavailable → the charge is
        retried by dunning, NEVER silently routed to a different gateway (charging a token on a gateway
        that did not issue it is exactly what pinning prevents). A legacy sub with no pin uses the
        active gateway — the best available answer for a row that predates pinning."""
        if not self._pinned_mode():
            return self._gw
        driver = (getattr(sub, "payment_gateway", "") or "").strip()
        if not driver:
            return self._gw
        return await self._gw_resolver(sub.tenant, driver)

    async def _gw_for_refund(self, pay) -> Any:
        """Refund through the gateway that CHARGED (Payment.provider) — in pinned mode a refund must
        reach the same gateway the money went to, or the provider id matches nothing."""
        if not self._pinned_mode():
            return self._gw
        driver = (getattr(pay, "provider", "") or "").strip()
        if not driver:
            return self._gw
        return await self._gw_resolver(pay.tenant, driver)

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
                          limits=None, trial_days=0, credit_percent_of_price: bool = False) -> Plan:
        return await self._plans.create(Plan(id=new_id("plan_"), name=name, prices=prices,
                                             features=features or [], limits=limits or {},
                                             trial_days=trial_days,
                                             credit_percent_of_price=bool(credit_percent_of_price)))

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
        allowed = ("name", "prices", "features", "limits", "trial_days", "active", "listed",
                   "credit_percent_of_price")
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

    # What a visitor may see of a plan. A WHITELIST, deliberately: a denylist would leak every
    # field added later, and the person adding it would have no reason to think about this list.
    # These are the terms of the offer — what it costs and what it includes — and nothing about
    # how the platform is run.
    PUBLIC_PLAN_FIELDS = ("id", "name", "prices", "features", "limits", "trial_days")
    PUBLIC_PRICE_FIELDS = ("interval", "amount_minor", "currency", "type", "usage_metric",
                           "included_units", "overage_minor", "includes_tax")

    async def public_plans(self) -> list[dict]:
        """The catalogue a pricing page can show to someone who has not signed up.

        `list_plans` requires a capability, which is correct for the panel and useless for a
        marketing page: a visitor has no principal, so the page that has to show prices was the one
        surface that could not read them. This is the same catalogue, projected down to the offer
        itself.

        Retired and unlisted plans are excluded — `listed=False` is the operator's own "do not show
        this" switch, and honouring it anywhere else while ignoring it here would make the switch a
        lie.
        """
        out = []
        for row in await self._plans.all(self._cfg["list_limit"]):
            if not (row.get("active", True) and row.get("listed", True)):
                continue
            plan = {k: row.get(k) for k in self.PUBLIC_PLAN_FIELDS if k in row}
            plan["prices"] = [{k: p.get(k) for k in self.PUBLIC_PRICE_FIELDS if k in p}
                              for p in (row.get("prices") or [])]
            out.append(plan)
        return out

    def _with_included_credit(self, row: dict, *, rule_pct: int = 0) -> dict:
        """A VIEW copy of a plan row with `included_credit_minor` added to each price, when the plan
        opts into a percent-of-price allowance. Computed LIVE from the current PLAN_CREDIT_PERCENT so a
        panel change shows immediately — per price, because each interval's price (hence its included
        amount) differs. Never mutates the stored row, and never touches a static-credits plan.

        `rule_pct` is the calling tenant's best cross-app discount for THIS plan; when the platform is
        discount-aware (BILLING_CREDIT_DISCOUNT_AWARE) the included amount is taken off the DISCOUNTED
        price, so the badge matches what the grant will actually be (a 100%-off plan shows 0 units)."""
        if not row.get("credit_percent_of_price"):
            return row
        pct = max(0, min(100, int(self._cfg.get("plan_credit_percent", 0) or 0)))
        aware = bool(self._cfg.get("credit_discount_aware")) and rule_pct > 0
        prices = []
        for p in (row.get("prices") or []):
            base = int(p.get("amount_minor", 0) or 0)
            net = base - self._pct_of(base, rule_pct) if aware else base
            prices.append({**p, "included_credit_minor": self._pct_of(net, pct)})
        return {**row, "prices": prices}

    async def list_plans(self, *, include_hidden: bool = False, tenant: str = "") -> list[dict]:
        rows = await self._plans.all(self._cfg["list_limit"])
        rows = ([r for r in rows] if include_hidden else
                [r for r in rows if r.get("listed", True) and r.get("active", True)])
        # When the platform is discount-aware AND we know the tenant, the included-units badge is taken
        # off the price the shop would actually pay (its best cross-app rule discount), so it matches
        # the grant. Otherwise it is list-price based (operator/internal view, or discount-unaware).
        rule_by_plan: dict[str, int] = {}
        if tenant and self._cfg.get("credit_discount_aware") and self._rules is not None:
            drules = await self._rules.all(self._cfg["list_limit"])
            if drules:
                now = self._clock()
                resolved = await self._entitlements.resolve(tenant, now)
                held = {str(k) for k, v in (resolved.get("limits") or {}).items() if v}
                for r in rows:
                    if r.get("credit_percent_of_price"):
                        p, _ = pricing.best_rule_percent(
                            drules, held_keys=held, target_keys=self._plan_target_keys(r), now=now)
                        rule_by_plan[r.get("id", "")] = p
        return [self._with_included_credit(r, rule_pct=rule_by_plan.get(r.get("id", ""), 0))
                for r in rows]

    async def upsert_coupon(self, **kw) -> Coupon:
        return await self._coupons.upsert(Coupon(created=iso(), **kw))

    async def list_coupons(self, *, status: str = "") -> list[dict]:
        """Every coupon, with WHY each one is or is not currently discounting.

        There was no way to list coupons at all: an operator had to remember every code they had
        ever created. Status is derived rather than stored, so a campaign that lapsed at midnight
        reads as expired without anything having had to wake up and mark it.
        """
        now = self._clock()
        rows = [_coupons.view(r, now) for r in await self._coupons.all(self._cfg["list_limit"])]
        if status:
            if status not in _coupons.STATUSES:
                raise ValueError(f"UNKNOWN_STATUS:{status}")
            rows = [r for r in rows if r["status"] == status]
        return sorted(rows, key=lambda r: r.get("created", ""), reverse=True)

    # What a CUSTOMER may see of a coupon. A whitelist, like the public plan fields: a denylist
    # would leak max_redemptions, redeemed and note the day a field is added.
    PUBLIC_OFFER_FIELDS = ("code", "type", "value", "currency", "starts_at", "expires_at")

    async def public_offers(self) -> list[dict]:
        """Active, PUBLIC campaigns, projected to customer-safe fields.

        The customer app had no way to learn a campaign existed — list_coupons is operator-only —
        so a discount had to be mirrored into a second store and kept in sync. This makes billing
        the single source of truth: the app reads and auto-applies, with no mirror to drift.

        Only coupons the operator marked `public` AND that are currently usable. A private code is
        never disclosed, and an expired or exhausted one is not offered — status is derived, so a
        campaign that lapsed at midnight drops out without anything having to mark it.
        """
        now = self._clock()
        out = []
        for row in await self._coupons.all(self._cfg["list_limit"]):
            if not row.get("public"):
                continue
            if _coupons.status(row, now) != _coupons.ACTIVE:
                continue
            out.append({k: row.get(k) for k in self.PUBLIC_OFFER_FIELDS if k in row})
        return out

    # A cross-tenant invoice view is a MONEY view, never a personal-data dragnet: it names the
    # tenant, the amount and the status, not the buyer. A platform operator scanning unpaid
    # invoices across every customer has no need for a thousand buyers' names and VAT numbers, and
    # a whitelist keeps a field added later from leaking into that view by default.
    # A money PROJECTION for a platform finance view — never buyer PII. subtotal_minor + tax_minor let
    # a partner commission be computed on the NET (VAT-inclusive prices otherwise pay the partner a
    # share of tax we remit to ZATCA); refunded_minor answers "what the shop actually kept paying"
    # (total_minor - refunded_minor) so a partial refund is not billed at full value.
    PLATFORM_INVOICE_FIELDS = ("id", "tenant", "number", "subscription_id", "status",
                               "subtotal_minor", "tax_minor", "total_minor", "refunded_minor",
                               "currency", "created", "paid_at")

    async def list_invoices_platform(self, *, status: str = "", subscription_id: str = "",
                                     tenant: str = "", since: str = "", until: str = "",
                                     limit: int = 100, cursor: str | None = None) -> dict:
        """Invoices ACROSS ALL TENANTS, newest first, for a platform finance view.

        The tenant-scoped listing answers "this customer's invoices"; this answers "every overdue
        invoice on the platform", which no per-tenant query gives without a tenant list. Gated on
        platform.billing and audited at the route.

        `tenant` narrows to one customer (still the cross-tenant path, just scoped). `since`/`until`
        bound the ISSUE DATE. The datastore is equality-only — it has no range operator — so the
        date window is applied as a bounded post-filter over the keyset-ordered pages: results are
        already newest-first, so paging stops as soon as a row falls before `since`, and the scan is
        capped so a wide window cannot become an unbounded read. The cursor stays a real
        (created, id) keyset, so two invoices issued in the same instant never drop a row off a page.
        """
        collected: list[dict] = []
        nxt = cursor
        for _ in range(self._cfg["platform_max_pages"]):
            rows, nxt = await self._invoices.browse_platform(
                where={"status": status, "subscription_id": subscription_id, "tenant": tenant},
                limit=limit, after=nxt)
            below_since = False
            for r in rows:
                created = r.get("created", "")
                if until and created > until:
                    continue                       # newer than the window; keep scanning down
                if since and created < since:
                    below_since = True             # created-desc: everything past here is older
                    break
                collected.append(r)
                if len(collected) >= limit:
                    break
            if below_since or not nxt or len(collected) >= limit:
                if below_since:
                    nxt = None                     # the window is exhausted, not merely this page
                break
        return {"invoices": [{k: r.get(k) for k in self.PLATFORM_INVOICE_FIELDS if k in r}
                             for r in collected[:limit]],
                "next_cursor": nxt}

    # A cross-tenant SUBSCRIPTION view, the counterpart to the invoice one: it answers "how many
    # new / active / canceled subscriptions this month" that no per-tenant query gives without a
    # tenant list. A lifecycle projection — plan, status, and the two timestamps — never buyer PII,
    # for the same reason the invoice view is a money projection: a platform operator counting churn
    # has no need for a thousand buyers' names, and a whitelist keeps a field added later from leaking.
    PLATFORM_SUBSCRIPTION_FIELDS = ("id", "tenant", "plan_id", "status", "payment_gateway",
                                    "created", "canceled_at")

    async def list_subscriptions_platform(self, *, status: str = "", plan_id: str = "",
                                          tenant: str = "", payment_gateway: str = "",
                                          since: str = "", until: str = "",
                                          limit: int = 100, cursor: str | None = None) -> dict:
        """Subscriptions ACROSS ALL TENANTS, newest first, for a platform lifecycle view.

        Mirrors list_invoices_platform exactly — gated on platform.billing and audited at the route,
        a mandatory filter, paginated and capped. `tenant` narrows to one customer (still the
        cross-tenant path). `since`/`until` bound the CREATION date; the datastore is equality-only,
        so the window is a bounded post-filter over the keyset-ordered (created desc) pages: paging
        stops as soon as a row falls before `since`, and the scan is capped so a wide window cannot
        become an unbounded read. The cursor stays a real (created, id) keyset so two subscriptions
        created in the same instant never drop a row off a page.
        """
        collected: list[dict] = []
        nxt = cursor
        for _ in range(self._cfg["platform_max_pages"]):
            rows, nxt = await self._subs.browse_platform(
                where={"status": status, "plan_id": plan_id, "tenant": tenant,
                       "payment_gateway": payment_gateway},
                limit=limit, after=nxt)
            below_since = False
            for r in rows:
                created = r.get("created", "")
                if until and created > until:
                    continue                       # newer than the window; keep scanning down
                if since and created < since:
                    below_since = True             # created-desc: everything past here is older
                    break
                collected.append(r)
                if len(collected) >= limit:
                    break
            if below_since or not nxt or len(collected) >= limit:
                if below_since:
                    nxt = None                     # the window is exhausted, not merely this page
                break
        return {"subscriptions": [{k: r.get(k) for k in self.PLATFORM_SUBSCRIPTION_FIELDS if k in r}
                                  for r in collected[:limit]],
                "next_cursor": nxt}

    async def get_coupon(self, code: str) -> dict | None:
        row = await self._coupons.get(code)
        return _coupons.view(row, self._clock()) if row else None

    async def update_coupon(self, code: str, **changes) -> dict:
        """Edit a live coupon without disturbing what customers have already redeemed."""
        allowed = ("type", "value", "currency", "starts_at", "expires_at", "max_redemptions",
                   "active", "public", "note")
        for key in changes:
            if key not in allowed:
                raise ValueError(f"COUPON_FIELD_NOT_EDITABLE:{key}")
        updated = await self._coupons.patch(code, changes)
        if updated is None:
            raise ValueError("COUPON_NOT_FOUND")
        return _coupons.view(updated.to_dict(), self._clock())

    async def delete_coupon(self, code: str) -> bool:
        return await self._coupons.remove(code)

    # ── cross-app discount rules ────────────────────────────────────────────────────
    @staticmethod
    def _clean_percent(value: Any) -> int:
        pct = int(value or 0)
        if not 0 <= pct <= 100:
            raise ValueError("DISCOUNT_PERCENT_OUT_OF_RANGE")   # 0-100 only; 100 = free
        return pct

    def _require_rules(self) -> DiscountRuleStore:
        if self._rules is None:
            raise ValueError("DISCOUNT_RULES_NOT_CONFIGURED")
        return self._rules

    async def create_discount_rule(self, *, owning_key: str, target_key: str, percent: int,
                                   active: bool = True, starts_at: float | None = None,
                                   expires_at: float | None = None, note: str = "") -> dict:
        store = self._require_rules()
        if not owning_key or not target_key:
            raise ValueError("DISCOUNT_KEYS_REQUIRED")
        rule = DiscountRule(id=new_id("dr_"), owning_key=str(owning_key), target_key=str(target_key),
                            percent=self._clean_percent(percent), active=bool(active),
                            starts_at=starts_at, expires_at=expires_at, note=note, created=iso())
        return (await store.upsert(rule)).to_dict()

    async def list_discount_rules(self) -> list[dict]:
        store = self._require_rules()
        rows = await store.all(self._cfg["list_limit"])
        return sorted(rows, key=lambda r: r.get("created", ""), reverse=True)

    async def update_discount_rule(self, rule_id: str, **changes) -> dict:
        store = self._require_rules()
        allowed = ("owning_key", "target_key", "percent", "active", "starts_at", "expires_at", "note")
        for key in changes:
            if key not in allowed:
                raise ValueError(f"DISCOUNT_FIELD_NOT_EDITABLE:{key}")
        if "percent" in changes and changes["percent"] is not None:
            changes["percent"] = self._clean_percent(changes["percent"])
        updated = await store.patch(rule_id, changes)
        if updated is None:
            raise ValueError("DISCOUNT_RULE_NOT_FOUND")
        return updated.to_dict()

    async def delete_discount_rule(self, rule_id: str) -> bool:
        return await self._require_rules().remove(rule_id)

    @staticmethod
    def _plan_target_keys(plan_row: dict) -> set[str]:
        """The keys a discount rule's target_key matches for a PLAN (no subscription yet): the plan's
        features, its limit keys, and its id."""
        keys: set[str] = {plan_row.get("id", "")}
        keys.update(plan_row.get("features") or [])
        keys.update((plan_row.get("limits") or {}).keys())
        return {str(k) for k in keys if k}

    async def discount_preview(self, tenant: str) -> dict[str, dict]:
        """Per-plan cross-app discount preview for a shop DECIDING whether to buy. Two sets:

          applied   — the shop ALREADY qualifies (holds the owning_key): the same ownership-only
                      resolution a renewal does right now (resolver + highest-percent-wins), charging
                      nothing. {plan_id: {"percent","owning_key"}}.
          potential — a MARKETING teaser: the best rule targeting a plan the shop does NOT yet qualify
                      for, computed WITHOUT the ownership filter, only for plans not already in
                      `applied`. owning_key names the app they must ALSO get ("X% off if you also get
                      Y"). {plan_id: {"percent","owning_key"}}.

        A plan absent from both simply has no rule. No coupon (none is typed at browse time)."""
        empty = {"applied": {}, "potential": {}}
        if self._rules is None:
            return empty
        rules = await self._rules.all(self._cfg["list_limit"])
        if not rules:
            return empty
        now = self._clock()
        resolved = await self._entitlements.resolve(tenant, now)
        held = {str(k) for k, v in (resolved.get("limits") or {}).items() if v}
        # For the teaser, ignore real ownership by pretending every rule's owner is held — so
        # best_rule_percent ranks purely by target match. Only used for plans NOT already applied,
        # where the shop holds none of the matching owners, so this never mislabels an owned rule.
        all_owners = {str(r.get("owning_key", "")) for r in rules}
        by_id = {str(r.get("id", "")): r for r in rules}
        applied: dict[str, dict] = {}
        potential: dict[str, dict] = {}
        for plan_row in await self._plans.all(self._cfg["list_limit"]):
            if not (plan_row.get("active", True) and plan_row.get("listed", True)):
                continue
            plan_id = plan_row.get("id", "")
            targets = self._plan_target_keys(plan_row)
            pct, rule_id = pricing.best_rule_percent(rules, held_keys=held, target_keys=targets, now=now)
            if pct > 0:
                applied[plan_id] = {"percent": pct,
                                    "owning_key": by_id.get(rule_id, {}).get("owning_key", "")}
                continue
            ppct, prid = pricing.best_rule_percent(rules, held_keys=all_owners, target_keys=targets,
                                                   now=now)
            if ppct > 0:
                potential[plan_id] = {"percent": ppct,
                                      "owning_key": by_id.get(prid, {}).get("owning_key", "")}
        return {"applied": applied, "potential": potential}

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
        if coupon_code:
            # Claimed HERE, once, when the code is attached to a subscription — not per billing
            # cycle. max_redemptions means "how many customers may use this"; counting per cycle
            # would make a monthly coupon consume its own cap every month and retire itself after
            # N months for a single customer.
            #
            # Nothing incremented `redeemed` before this line existed, so the cap an operator set
            # was never enforced at all.
            # A plan-scoped coupon is REFUSED (loud) for a subscription to a different plan, rather
            # than silently discounting nothing and charging full price — a customer who typed a real
            # code for the wrong plan sees why. Checked BEFORE redeem, so a mismatch never consumes a
            # redemption. Empty plan_id = applies to any plan (unchanged behaviour).
            if coupon is not None and coupon.plan_id and coupon.plan_id != plan_id:
                raise ValueError("COUPON_PLAN_MISMATCH")
            reason = _coupons.status(coupon.to_dict() if coupon else None, now)
            if reason != _coupons.ACTIVE:
                raise ValueError(f"COUPON_{reason.upper()}")
            # ATOMIC redeem: the cap is enforced inside a CAS, so two concurrent signups cannot both
            # slip past max_redemptions (the status() check above races otherwise).
            applied, _count = await self._coupons.redeem(
                coupon_code, max_redemptions=getattr(coupon, "max_redemptions", None))
            if not applied:
                raise ValueError("COUPON_MAX_REDEMPTIONS")
        if coupon and coupon.type == "trial":
            trial_days += int(coupon.value)
        trial_end = now + trial_days * 86400 if trial_days else 0.0
        price_snap, limits_snap = self._snapshot(plan, interval)   # lock the signup terms
        # The period boundary decides when the entitlement lapses and when the sweep next charges.
        #   once     — a one-time purchase does NOT renew and has no period to lapse. It gets a ZERO
        #              period end so the resolver's "no period end ⇒ never lapses" rule applies (a
        #              non-zero `now` would expire the pack the next day), and due() skips it so the
        #              sweep never re-charges it.
        #   trialing — the period end IS the trial end (Stripe/Paddle semantics): the FIRST charge is
        #              raised when the trial ends and the cycle moves the period forward a full
        #              interval then. Setting a full interval here gave a 14-day trial 30 free days,
        #              because trial_end was written and never read.
        #   else     — a full interval from now.
        first_period_prorated = False
        if interval == "once":
            period_end, status = 0.0, "active"
        elif trial_days:
            period_end, status = trial_end, "trialing"
        else:
            interval_secs = _INTERVAL.get(interval, _INTERVAL["month"])
            period_end, status = now + interval_secs, "active"
            # ALIGN a second+ subscription onto the tenant's EXISTING billing grid so it renews on the
            # same day as the first, instead of opening its own cycle (the escalated defect: buy on the
            # 3rd and the 17th → charged twice a month for ever). One indexed read of the tenant's live
            # billable subscriptions; only same-interval, renewing ones are a valid grid reference. When
            # one exists, the first period ends at the shared grid boundary — a PARTIAL period, prorated
            # on the first cycle — never a full interval from now. A tenant's FIRST subscription finds no
            # reference and is completely unchanged.
            existing = await self._subs.billable_for(tenant, _BILLABLE)
            refs = [float(r.get("current_period_end") or 0) for r in existing
                    if r.get("interval") == interval and r.get("interval") != "once"
                    and r.get("status") != "trialing" and float(r.get("current_period_end") or 0) > 0]
            if refs:
                aligned = periods.aligned_period_end(now, interval_secs, min(refs))
                # Money-safe direction, asserted explicitly: alignment may only SHORTEN the first
                # period (the next grid boundary is always within one interval of now). It must never
                # lengthen it — that would hand out free time — so we align only when strictly shorter.
                if aligned < period_end:
                    period_end = aligned
                    first_period_prorated = True
            # RUNTIME INVARIANT (the guard the escalation valued above the fix): after this signup the
            # tenant must have ONE renewal day for this interval. Projecting the new subscription onto
            # the existing set, more than one distinct period-end means an alignment gap — a drift a
            # product built on the platform must be able to SEE, so it is logged, loudly, rather than
            # discovered from a customer charged twice a month. Never raises: a legacy tenant that was
            # already misaligned must still be able to subscribe. `distinct_renewing_period_ends` is the
            # same pure check any product can assert at boot.
            projected = [*existing, {"interval": interval, "status": status,
                                     "current_period_end": period_end}]
            ends = periods.distinct_renewing_period_ends(projected, interval)
            if len(ends) > 1:
                _log.warning("billing_period_misaligned",
                             extra={"tenant": tenant, "interval": interval,
                                    "distinct_period_ends": sorted(ends)})
        sub = Subscription(
            id=new_id("sub_"), tenant=tenant, plan_id=plan_id, interval=interval, quantity=quantity,
            coupon_code=coupon_code, payment_token=payment_token, buyer_name=buyer_name,
            # Pin the gateway this subscription is born on, so pinned-mode renewals charge HERE even
            # after the active driver is switched. Harmless (unused) in active mode.
            payment_gateway=getattr(self._gw, "driver", ""),
            buyer_vat=buyer_vat, buyer_cr=buyer_cr,
            price_snapshot=price_snap, limits_snapshot=limits_snap, terms_version=iso(),
            status=status, current_period_start=now, current_period_end=period_end,
            trial_end=trial_end, first_period_prorated=first_period_prorated, created=iso())
        return await self._subs.save(sub)

    async def change_plan(self, sub_id: str, *, new_plan_id: str, interval: str | None = None,
                          quantity: int | None = None) -> dict[str, Any]:
        """Change a subscription's plan, Stripe-style — and NEVER produce a credit.

        UPGRADE (new price >= current): the prorated difference is CHARGED FIRST, and the plan and
        entitlements flip ONLY once that charge clears. A decline changes nothing (and does NOT enter the
        async dunning cycle — this is a synchronous user action, so it fails synchronously). An upgrade
        also supersedes any scheduled downgrade.

        DOWNGRADE (new price < current): nothing changes now — the shop keeps the more expensive plan it
        already paid for, at full value, until the period ends — and the target is SCHEDULED for the next
        renewal (pending_plan_id). At that renewal run_cycle adopts it and invoices the new plan at full
        price, no proration, no credit.
        """
        sub = await self._subs.fetch(sub_id)
        if sub is None:
            raise ValueError("SUB_NOT_FOUND")
        old_plan = await self._plans.fetch(sub.plan_id)
        new_plan = await self._plans.fetch(new_plan_id)
        if new_plan is None:
            raise ValueError("PLAN_NOT_FOUND")
        new_interval = interval or sub.interval
        new_qty = quantity or sub.quantity
        old_price = pricing.line_amount(self._effective_price(sub, old_plan), quantity=sub.quantity)
        new_price = pricing.line_amount(self._price_for(new_plan, new_interval), quantity=new_qty)

        if new_price < old_price:
            # ── DOWNGRADE: schedule at renewal, touch nothing now ──────────────────────────────
            sub.pending_plan_id, sub.pending_interval, sub.pending_quantity = \
                new_plan_id, new_interval, new_qty
            await self._subs.save(sub)
            return {"subscription_id": sub.id, "applied": "scheduled_downgrade",
                    "pending_plan_id": new_plan_id, "effective": "next_renewal", "proration_minor": 0}

        # ── UPGRADE (or lateral): charge the difference FIRST, flip only on success ─────────────
        delta = pricing.proration_delta(old_price, new_price, period_start=sub.current_period_start,
                                        period_end=sub.current_period_end, now=now_ts())
        invoice_id = ""
        if delta > 0:
            inv = await self._charge_upgrade(sub, new_plan, new_interval, new_qty, delta)  # may raise
            invoice_id = inv.id
        # Only now — the money cleared (or there was nothing to charge) — does the plan take effect.
        sub.plan_id, sub.interval, sub.quantity = new_plan_id, new_interval, new_qty
        sub.price_snapshot, sub.limits_snapshot = self._snapshot(new_plan, new_interval)  # customer chose these
        sub.terms_version = iso()
        # An upgrade supersedes a scheduled downgrade — the shop went up instead of down.
        sub.pending_plan_id, sub.pending_interval, sub.pending_quantity = "", "", 0
        await self._subs.save(sub)
        return {"subscription_id": sub.id, "applied": "upgraded", "proration_minor": delta,
                "invoice_id": invoice_id}

    async def _charge_upgrade(self, sub: Subscription, new_plan: Plan, new_interval: str,
                              new_qty: int, delta: int) -> Invoice:
        """Charge an upgrade's prorated difference NOW, and persist a paid invoice ONLY on success.

        Charge-first, persist-on-success: a decline burns no invoice number and leaves no unpaid row.
        The gateway idempotency key is deterministic per (subscription, period, target plan, amount), so
        a retried request — or a crash after the charge but before we persisted — dedupes at the gateway
        instead of double-charging. On success the invoice runs through the SAME _mark_paid seam every
        other paid invoice does (doc_type / VAT / e-invoice / ledger / grants / event); it never touches
        the dunning path, because a synchronous user action must fail synchronously, not silently retry.
        """
        if not (sub.payment_token or "").strip():
            raise UpgradeChargeFailed("NO_PAYMENT_METHOD")
        price = self._price_for(new_plan, new_interval)
        currency = price.get("currency", self._cfg["currency"])
        taxable, tax, total = self._split_tax(
            delta, price.get("includes_tax", False),
            reverse_charge=bool(sub.buyer_vat) and self._cfg.get("reverse_charge", False))
        idem = f"upgrade:{sub.id}:{int(sub.current_period_end)}:{new_plan.id}:{total}"
        gw = await self._gw_for_sub(sub)     # pinned mode: the sub's own gateway, not the active one
        try:
            charge = gw.charge(amount_minor=total, currency=currency, token=sub.payment_token,
                               idempotency_key=idem, description="Plan change — upgrade")
        except GatewayUnavailable as ex:
            raise UpgradeChargeUnavailable(str(ex)) from ex
        if not charge.ok:
            raise UpgradeChargeFailed(charge.error or "DECLINED")
        inv = Invoice(id=new_id("inv_"), tenant=sub.tenant,
                      number=await self._invoices.next_number(sub.tenant), subscription_id=sub.id,
                      lines=[{"description": "Plan change — upgrade", "quantity": 1,
                              "unit_minor": delta, "amount_minor": taxable}],
                      subtotal_minor=taxable, tax_minor=tax, total_minor=total, currency=currency,
                      status="open", created=iso())
        await self._invoices.save(inv)
        pay = Payment(id=new_id("pay_"), tenant=sub.tenant, invoice_id=inv.id, amount_minor=total,
                      currency=currency, provider=charge.provider, provider_id=charge.provider_id,
                      status="succeeded", created=iso())
        await self._payments.save(pay)
        await self._mark_paid(inv)                          # doc_type, VAT, e-invoice, ledger, event
        return inv

    # ── one-off wallet top-up ────────────────────────────────────────────────────
    async def wallet_topup(self, *, tenant: str, amount_minor: int, payment_token: str,
                           fund: str = "", currency: str = "") -> dict[str, Any]:
        """Charge an arbitrary one-off amount and credit it to a wallet fund — a manual top-up of a
        tenant's prepaid balance (e.g. AI-usage credit), NOT a subscription.

        Reuses the whole paid-invoice machinery instead of a parallel one-off path: it raises an
        invoice carrying `grant_minor`+`grant_fund`, charges the gateway, and lets _mark_paid credit
        the wallet through the SAME durable, idempotent grant outbox a plan renewal uses — so a
        redelivered webhook or a reconciliation sweep credits the fund exactly once, and a refund
        claws it back through the same seam.

        Charge-first, persist-on-success (like an upgrade): a decline burns no invoice number and
        leaves no unpaid row. TAX: a top-up is a payment on account — no VAT is charged here by
        default (BILLING_WALLET_TOPUP_TAX_MODE='receipt'); VAT is applied when the credit is SPENT
        (the wallet fund's taxable flag), so charging it at top-up too would double-tax. A platform
        that needs a tax invoice at top-up sets the mode to 'tax_invoice'."""
        amount = int(amount_minor)
        if amount <= 0:
            raise ValueError("AMOUNT_MUST_BE_POSITIVE")
        cap = int(self._cfg.get("wallet_topup_max_minor", 0) or 0)
        if cap and amount > cap:
            raise ValueError("AMOUNT_EXCEEDS_MAX")
        if not (payment_token or "").strip():
            raise UpgradeChargeFailed("NO_PAYMENT_METHOD")
        fund = (fund or self._cfg.get("wallet_topup_fund", "") or "").strip()
        if not fund:
            raise ValueError("FUND_REQUIRED")
        currency = currency or self._cfg["currency"]
        tax_invoice = self._cfg.get("wallet_topup_tax_mode", "receipt") == "tax_invoice"
        if tax_invoice:
            # VAT added on top of the requested amount; the buyer pays gross, the fund is credited the
            # gross too (the credit they bought). _mark_paid then issues a real tax invoice.
            taxable, tax, total = self._split_tax(amount, includes_tax=False)
            force_doc = None
        else:
            taxable, tax, total = amount, 0, amount
            force_doc = "receipt"
        meta: dict[str, Any] = {"grant_minor": total, "grant_fund": fund, "topup": True}
        if force_doc:
            meta["force_doc_type"] = force_doc
        idem = f"topup:{tenant}:{payment_token}:{total}"
        gw = self._gw                       # a top-up is a fresh charge; no per-sub gateway pinning
        try:
            charge = gw.charge(amount_minor=total, currency=currency, token=payment_token,
                               idempotency_key=idem, description="Wallet top-up")
        except GatewayUnavailable as ex:
            raise UpgradeChargeUnavailable(str(ex)) from ex
        if not charge.ok:
            raise UpgradeChargeFailed(charge.error or "DECLINED")
        inv = Invoice(id=new_id("inv_"), tenant=tenant,
                      number=await self._invoices.next_number(tenant), subscription_id="",
                      lines=[{"description": "Wallet top-up", "quantity": 1,
                              "unit_minor": taxable, "amount_minor": taxable}],
                      subtotal_minor=taxable, tax_minor=tax, total_minor=total, currency=currency,
                      status="open", created=iso(), meta=meta)
        await self._invoices.save(inv)
        pay = Payment(id=new_id("pay_"), tenant=tenant, invoice_id=inv.id, amount_minor=total,
                      currency=currency, provider=charge.provider, provider_id=charge.provider_id,
                      status="succeeded", created=iso())
        await self._payments.save(pay)
        await self._mark_paid(inv)          # credits `fund` via the grant outbox; doc_type frozen
        return {"topup_id": inv.id, "invoice_id": inv.id, "invoice_number": inv.number,
                "status": "succeeded", "amount_minor": total, "fund": fund, "currency": currency,
                "doc_type": inv.doc_type, "provider_id": charge.provider_id}

    async def cancel_scheduled_change(self, sub_id: str) -> dict[str, Any]:
        """Undo a scheduled downgrade before it takes effect — the shop stays on its current plan."""
        sub = await self._subs.fetch(sub_id)
        if sub is None:
            raise ValueError("SUB_NOT_FOUND")
        had = bool(sub.pending_plan_id)
        sub.pending_plan_id, sub.pending_interval, sub.pending_quantity = "", "", 0
        await self._subs.save(sub)
        return {"subscription_id": sub.id, "cancelled_scheduled_change": had}

    async def undo_cancel(self, sub_id: str) -> dict[str, Any]:
        """Undo a scheduled cancellation on a subscription that has NOT yet ended — the shop changed
        its mind.

        `cancel(at_period_end=True)` leaves a subscription LIVE (active/trialing/past_due) with
        cancel_at_period_end=True, and reactivate() deliberately refuses anything but paused -> active.
        So this is the missing self-service undo, the mirror of cancel_scheduled_change: it clears the
        flag for any subscription that is not already terminal — a `canceled` one has ended and is never
        resurrected here — and is an idempotent no-op (never an error) when nothing is scheduled to
        cancel, so a double click or a stale button is harmless.
        """
        sub = await self._subs.fetch(sub_id)
        if sub is None:
            raise ValueError("SUB_NOT_FOUND")
        undone = sub.status != "canceled" and sub.cancel_at_period_end
        if undone:
            sub.cancel_at_period_end = False
            await self._subs.save(sub)
        return {"subscription_id": sub.id, "undone_cancellation": bool(undone)}

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

    async def update_payment_method(self, sub_id: str, payment_token: str) -> Subscription | None:
        """Replace the stored payment token on a subscription — the self-service recovery from an
        expired or declined card that was missing.

        payment_token was writable exactly once, at subscribe(), so dunning retried the SAME failing
        token until the subscription was suspended — involuntary churn a tenant could not fix without a
        support ticket. This is the one write that lets them fix it themselves. NO status transition and
        NO retry here: the next scheduled dunning pass spends the new token, exactly as it does today —
        this call only changes what that pass will use. Returns None for an unknown id (the route 404s);
        an empty token is refused, because a blank one would guarantee the next charge fails.
        """
        if not (payment_token or "").strip():
            raise ValueError("PAYMENT_TOKEN_REQUIRED")
        sub = await self._subs.fetch(sub_id)
        if sub is None:
            return None
        sub.payment_token = payment_token
        # Re-tokenising moves the subscriber onto the CURRENTLY-active gateway (the token was minted
        # there). Re-pin so pinned-mode renewals follow — this is the one path by which a subscriber
        # migrates to a newly-activated gateway, and it is their own deliberate action.
        sub.payment_gateway = getattr(self._gw, "driver", "")
        return await self._subs.save(sub)

    async def pause(self, sub_id: str) -> Subscription | None:
        """Active → paused. Refuses anything else.

        Both of these used to set the status unconditionally, which was survivable while nothing
        called them and is not once there is a route. Pausing a CANCELED subscription would move
        it out of the terminal state, and the pair below is worse.
        """
        return await self._set_status(sub_id, "paused", allowed_from=("active",))

    async def force_suspend(self, sub_id: str) -> Subscription | None:
        """A deliberate ADMIN stop of one subscription → paused, from any live state.

        Distinct from pause() (self-service, active→paused only): this is the operator override for a
        subscriber stranded on an old gateway who has not re-entered payment — stopping their renewals
        one at a time, on purpose. Refuses only a terminal `canceled` sub (nothing to suspend);
        idempotent on an already-paused one. reactivate() is the reverse once they update payment."""
        return await self._set_status(sub_id, "paused",
                                      allowed_from=("active", "trialing", "past_due", "paused"))

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
            sub.canceled_at = sub.canceled_at or iso()   # stamp the moment it actually ended
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
        if status == "canceled":
            sub.canceled_at = sub.canceled_at or iso()   # any path to canceled carries its timestamp
        return await self._subs.save(sub)

    async def record_usage(self, *, tenant: str, subscription_id: str, metric: str, quantity: int) -> dict:
        return await self._usage.record(tenant, subscription_id, metric, quantity)

    # ── billing cycle ─────────────────────────────────────────────────────────────
    @staticmethod
    def _sub_target_keys(sub, plan) -> set[str]:
        """The feature/plan keys that identify WHICH app this subscription is — what a discount rule's
        target_key matches against: the plan's features, the plan id, and the keys the plan grants."""
        keys: set[str] = {sub.plan_id}
        if plan is not None:
            keys.update(plan.features or [])
            keys.update((plan.limits or {}).keys())
        keys.update((getattr(sub, "limits_snapshot", None) or {}).keys())
        return {str(k) for k in keys if k}

    async def _rule_discount(self, sub, plan, subtotal: int) -> tuple[int, str]:
        """The best cross-app discount (minor units, MAX composition) for this subscription's renewal,
        and the rule id. (0, "") when the feature is off or nothing applies. Ownership is read from the
        EntitlementResolver — the same source of truth billing uses — so a rule triggers on what the
        tenant holds RIGHT NOW, and a lapse/gain moves only the next cycle."""
        if self._rules is None or subtotal <= 0:
            return 0, ""
        rules = await self._rules.all(self._cfg["list_limit"])
        if not rules:
            return 0, ""
        resolved = await self._entitlements.resolve(sub.tenant, self._clock())
        held = {str(k) for k, v in (resolved.get("limits") or {}).items() if v}
        targets = self._sub_target_keys(sub, plan)
        return pricing.rule_discount(rules, subtotal, held_keys=held, target_keys=targets,
                                     now=self._clock())

    def _pct_of(self, base_minor: int, pct: int) -> int:
        return int((_Decimal(max(0, base_minor)) * _Decimal(pct) / 100).quantize(
            _Decimal(1), _ROUND_HALF_UP))

    def _granted_allowance(self, plan, *, list_base: int, net_base: int) -> int:
        """The included wallet allowance to grant for this cycle, in minor units.

        Default: the plan's static limits[PLAN_CREDIT_LIMIT_KEY]. A plan that opts in with
        credit_percent_of_price=True instead grants a PERCENT of its price (PLAN_CREDIT_PERCENT, a
        runtime-editable global knob) — so an operator can change "how much is included" with no
        redeploy or reseed. Percent clamped 0-100.

        WHICH PRICE the percent is taken from is per-platform (BILLING_CREDIT_DISCOUNT_AWARE):
          off  (default) — the plan's LIST price (list_base): a discount never shrinks the allowance.
          on             — the amount ACTUALLY PAID this cycle (net_base = subtotal after the rule/
                           coupon discount): a 100%-off plan grants 0 units, matching 0 paid. This
                           closes the hole where a fully-discounted plan still handed out full-price
                           free units."""
        if plan is None:
            return 0
        if getattr(plan, "credit_percent_of_price", False):
            pct = max(0, min(100, int(self._cfg.get("plan_credit_percent", 0) or 0)))
            base = net_base if self._cfg.get("credit_discount_aware") else list_base
            return self._pct_of(int(base or 0), pct)
        return int((plan.limits or {}).get(self._cfg["plan_credit_limit_key"], 0) or 0)

    async def _compute_renewal(self, sub) -> dict[str, Any]:
        """Price ONE subscription's renewal — adopt a scheduled downgrade, apply grandfathered price,
        proration, usage, coupon and tax — and return the invoice LINE + rolled-up amounts + the
        plan's granted allowance. NO side effects on the invoice/charge/period: the SAME priced unit is
        used by a per-subscription invoice AND by a consolidated (one-per-tenant) invoice, so both cut
        identical money. Mutates only `sub`'s plan fields when a pending downgrade is adopted."""
        # A SCHEDULED DOWNGRADE takes effect at THIS renewal: adopt the new plan BEFORE pricing, so the
        # invoice is the new plan at its FULL price for the new period — no proration, no credit, the
        # shop having had full value from the old plan through the period it paid for. `cancel_at_period_end`
        # WINS: an ending subscription does not renew onto a new plan. A pending plan that was since
        # deleted is simply cleared (the subscription stays as it is), never a crash.
        if sub.pending_plan_id and not sub.cancel_at_period_end:
            pending_plan = await self._plans.fetch(sub.pending_plan_id)
            if pending_plan is not None:
                sub.plan_id = sub.pending_plan_id
                sub.interval = sub.pending_interval or sub.interval
                sub.quantity = sub.pending_quantity or sub.quantity
                sub.price_snapshot, sub.limits_snapshot = self._snapshot(pending_plan, sub.interval)
                sub.terms_version = iso()
            sub.pending_plan_id, sub.pending_interval, sub.pending_quantity = "", "", 0
        plan = await self._plans.fetch(sub.plan_id)
        price = self._effective_price(sub, plan)          # grandfathered signup price
        currency = price.get("currency", self._cfg["currency"])
        usage_units = 0
        if price.get("usage_metric"):
            usage_units = await self._usage.aggregate(sub.id, price["usage_metric"],
                                                      sub.current_period_start, sub.current_period_end)
        subtotal = pricing.line_amount(price, quantity=sub.quantity, usage_units=usage_units)
        proration = None
        if getattr(sub, "first_period_prorated", False):
            interval_secs = _INTERVAL.get(sub.interval, _INTERVAL["month"])
            period_secs = round(sub.current_period_end - sub.current_period_start)
            frac = _Decimal(max(0, period_secs)) / _Decimal(interval_secs) if interval_secs > 0 else _Decimal(1)
            recurring = pricing.recurring_component(price, quantity=sub.quantity)
            metered = subtotal - recurring                    # usage/overage: charged in full
            subtotal = pricing.prorate(recurring, frac) + metered
            proration = {"fraction": str(frac), "period_seconds": max(0, period_secs),
                         "interval_seconds": interval_secs,
                         "recurring_full_minor": recurring, "recurring_charged_minor": subtotal - metered}
        coupon = await self._coupons.fetch(sub.coupon_code) if sub.coupon_code else None
        coupon_disc = pricing.coupon_discount(coupon.to_dict() if coupon else None, subtotal,
                                              now=self._clock())
        # Cross-app rule discount, evaluated LIVE this cycle against what the tenant holds NOW (so
        # gaining/losing an app changes the NEXT renewal only, never a past invoice). The larger of the
        # coupon and the best rule wins — never summed (product decision 2026-09-11), so combined
        # discounts can't silently reach 100%.
        rule_disc, rule_id = await self._rule_discount(sub, plan, subtotal)
        discount = max(coupon_disc, rule_disc)
        taxable, tax, total = self._split_tax(subtotal - discount, price.get("includes_tax", False),
                                              reverse_charge=bool(sub.buyer_vat) and self._cfg.get("reverse_charge", False))
        line = {"description": (plan.name if plan else "Subscription"),
                "quantity": sub.quantity, "unit_minor": price.get("amount_minor", 0),
                "amount_minor": taxable}
        if proration is not None:
            line["description"] += " (prorated first period)"
            line["proration"] = proration
        granted = self._granted_allowance(
            plan, list_base=int(price.get("amount_minor", 0) or 0), net_base=int(subtotal - discount))
        return {"line": line, "taxable": taxable, "discount": discount, "tax": tax, "total": total,
                "currency": currency, "granted": granted, "plan": plan}

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
        # ATOMIC claim, not get-then-mark: two concurrent runs of the same cycle would each create a
        # NEW invoice id, so the gateway idempotency key would differ and the card would be charged
        # TWICE. The claim lets exactly one run proceed; a duplicate replays the invoice or retries.
        proceed, prior = await self._idempo.claim(key)
        if prior is not None:
            return await self._invoices.fetch(prior["invoice_id"]) or Invoice(id="", tenant=sub.tenant)
        if not proceed:
            raise ValueError("CYCLE_IN_PROGRESS")     # a concurrent run holds it; the sweep retries
        r = await self._compute_renewal(sub)              # adopt pending + price (no side effects)
        inv = Invoice(id=new_id("inv_"), tenant=sub.tenant, number=await self._invoices.next_number(sub.tenant),
                      subscription_id=sub.id, lines=[r["line"]], subtotal_minor=r["taxable"],
                      discount_minor=r["discount"], tax_minor=r["tax"], total_minor=r["total"],
                      currency=r["currency"], status="open", created=iso(),
                      # period_end travels WITH the invoice so a `reset` grant expires at the end
                      # of the period it was sold for, even if delivery is retried days later.
                      meta={"grant_minor": r["granted"],
                            "period_end": sub.current_period_end} if r["granted"] > 0 else {})
        await self._invoices.save(inv)
        sub.current_period_start = sub.current_period_end
        sub.current_period_end += _INTERVAL.get(sub.interval, _INTERVAL["month"])
        # The partial first period is now billed; every cycle after is a full interval on the shared
        # grid. Clear the flag so a later cycle can never re-prorate a full period.
        sub.first_period_prorated = False
        if sub.status == "trialing":
            sub.status = "active"
        await self._subs.save(sub)
        await self._charge_invoice(inv, sub.payment_token, sub)
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
        # Atomic claim — same reasoning as run_cycle: a concurrent double run would double-charge the
        # overage on two distinct invoice ids.
        proceed, prior = await self._idempo.claim(key)
        if prior is not None:
            return None                                   # already billed this period (unchanged contract)
        if not proceed:
            raise ValueError("OVERAGE_IN_PROGRESS")
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
        await self._charge_invoice(inv, sub.payment_token, sub)
        await self._idempo.mark(key, {"invoice_id": inv.id, "overage": over})
        return await self._invoices.fetch(inv.id) or inv

    async def _charge_invoice(self, inv: Invoice, token: str, sub=None) -> None:
        pay = Payment(id=new_id("pay_"), tenant=inv.tenant, invoice_id=inv.id,
                      amount_minor=inv.total_minor, currency=inv.currency, created=iso())
        if inv.total_minor <= 0:                            # nothing to charge (100% coupon / trial)
            pay.status = "succeeded"
            await self._payments.save(pay)
            await self._mark_paid(inv)
            return
        # RENEWAL BY WALLET DEBIT — a subscription pinned to the "wallet" gateway collects from the
        # customer's prepaid balance instead of a card (for gateways with no recurring, e.g.
        # TuwaiqPay). Decided before gateway resolution: there is no "wallet" payment adapter to
        # build. Returns True when it handled the invoice (paid or suspended); False to fall through
        # to the normal gateway path.
        if await self._charge_from_wallet(inv, pay, sub):
            return
        # pinned mode: charge the subscription's OWN gateway (fail-closed if it cannot build), so a
        # renewal after a driver switch still reaches the gateway that issued the token.
        gw = await self._gw_for_sub(sub) if sub is not None else self._gw
        try:
            charge = gw.charge(amount_minor=inv.total_minor, currency=inv.currency,
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

    # ── renewal by wallet debit ─────────────────────────────────────────────────
    def _wallet_renewal_fund(self, sub) -> str | None:
        """The fund a wallet renewal debits for THIS subscription, or None to use the normal gateway.

        Reusable infrastructure: the platform turns the feature on and names the fund; a subscription
        OPTS IN by being pinned to payment_gateway == "wallet". The wallet may hold many funds for
        many purposes — billing does not decide their number or meaning, it only debits the one the
        platform named. An enabled, opted-in sub with NO fund configured is a misconfiguration and
        returns "" so the caller fails the charge loudly (it must never guess a fund)."""
        if not self._cfg.get("wallet_renewal_enabled") or self._wallet is None or sub is None:
            return None
        if (getattr(sub, "payment_gateway", "") or "").strip().lower() != "wallet":
            return None
        return self._cfg.get("wallet_renewal_fund", "") or ""

    async def _charge_from_wallet(self, inv: Invoice, pay: Payment, sub) -> bool:
        """Collect a renewal by debiting the customer's wallet. Returns True when it OWNED the
        outcome (paid, suspended, or queued for retry), False to fall through to the gateway.

        The debit is the invoice total EXACTLY — VAT is already inside it (billing computed the tax
        invoice). It is a fund-scoped debit, never a re-priced spend: a spend would re-apply VAT and
        double-tax the customer. `source=inv.id` makes it idempotent (the outbox/sweep may re-offer
        the same invoice), and it is scoped to the tenant by the wallet. allow_negative is False —
        a renewal must never push a prepaid balance below zero."""
        fund = self._wallet_renewal_fund(sub)
        if fund is None:
            return False
        if not fund:
            # Opted in but no fund named. Fail LOUD and observable — never silently take free service
            # and never guess a fund. The sweep records this per-sub; an operator sees the misconfig.
            _log.error("wallet_renewal_unconfigured tenant=%s subscription=%s invoice=%s",
                       inv.tenant, getattr(sub, "id", ""), inv.id)
            raise WalletUnavailable("BILLING_WALLET_RENEWAL_FUND is not configured")
        pay.provider = "wallet"
        pay.provider_id = inv.id
        try:
            await self._wallet.debit(tenant=inv.tenant, fund=fund, amount_minor=inv.total_minor,
                                     source=inv.id, reason=inv.number, allow_negative=False)
        except WalletRefused:
            # The balance cannot cover it (or the fund is mis-set) — permanent for this renewal: a
            # prepaid balance does not grow back on its own, so retrying for ever hides it. Suspend
            # IMMEDIATELY and signal a top-up, rather than entering the card-decline dunning loop.
            pay.status = "failed"
            pay.error = "WALLET_INSUFFICIENT"
            pay.next_retry_at = None
            await self._payments.save(pay)
            await self._suspend_wallet_insufficient(inv, sub)
            return True
        except WalletUnavailable as ex:
            # The wallet was unreachable — genuinely retryable (unlike an empty balance). Dunning
            # will re-offer the invoice; a transient outage must not suspend a paying customer.
            pay.status = "failed"
            pay.error = str(ex)
            await self._dunning(pay)
            await self._payments.save(pay)
            return True
        pay.status = "succeeded"
        await self._payments.save(pay)
        await self._mark_paid(inv)
        return True

    async def _suspend_wallet_insufficient(self, inv: Invoice, sub) -> None:
        """Suspend on an insufficient wallet balance, OBSERVABLY. A wallet renewal that quietly
        failed would look like free service; a WARN spike and a distinct event are the signal that
        customers need to top up (and that a mis-set fund would refuse EVERY renewal at once)."""
        if sub is not None:
            sub.status = "past_due"
            await self._subs.save(sub)
        _log.warning("wallet_renewal_insufficient tenant=%s subscription=%s invoice=%s amount=%s %s",
                     inv.tenant, getattr(sub, "id", ""), inv.id, inv.total_minor, inv.currency)
        if self._events is not None:
            await self._events.emit(_events.SUBSCRIPTION_RENEWAL_WALLET_INSUFFICIENT, {
                "tenant": inv.tenant, "subscription_id": getattr(sub, "id", ""),
                "invoice_id": inv.id, "amount_minor": inv.total_minor, "currency": inv.currency,
            })

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
        # WHAT THIS INVOICE IS, decided ONCE and FROZEN here. The mode alone is not enough: a tax
        # invoice is only issued when the profile behind it can back one (a real calculator, a rate
        # above zero, a seller registration number). If the mode says tax but the profile cannot,
        # the document degrades to a RECEIPT — the true, lesser thing — rather than a void tax
        # invoice. The decided type is stamped on the invoice so every later render follows the
        # INVOICE, not whatever the setting happens to be then (a mode switched, or switched back,
        # after issue must never rewrite a document already produced). See app/core/tax_profile.py.
        seller = self._cfg.get("seller", {}) or {}
        forced = (inv.meta or {}).get("force_doc_type")
        if forced == "receipt":
            # A wallet top-up (and any payment-on-account) is a RECEIPT, never a tax invoice: no VAT
            # is charged at top-up because the wallet applies VAT when the credit is SPENT (the fund's
            # taxable flag). Charging it here too would double-tax. Forced on the invoice so the
            # decision is frozen with the document, not re-derived from the live document_mode.
            inv.doc_type = "receipt"
        else:
            inv.doc_type = tax_profile.effective_doc_type(
                self._cfg.get("document_mode", "receipt"),
                tax_driver=self._cfg.get("tax_driver"), vat_rate=self._cfg.get("vat_rate", "0"),
                seller_vat=seller.get("vat_number", ""),
                einvoicer_driver=self._cfg.get("einvoicer_driver"))
        if inv.doc_type == "tax_invoice":
            # Freeze the seller's legal identity onto the invoice, so re-rendering a delivered tax
            # invoice always shows the registration number it was ISSUED with.
            inv.seller = {k: v for k, v in seller.items() if v}
            buyer = await self._buyer_for(inv)
            inv.einvoice = self._einv.finalize(tenant=inv.tenant,
                                               invoice=self._einvoice_payload(inv, buyer))
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
                # The fund is per-invoice-overridable (a wallet top-up credits its OWN fund, e.g.
                # "topup", not the plan grant fund) and falls back to the configured plan grant fund.
                fund = (inv.meta or {}).get("grant_fund") or self._cfg["grant_fund"]
                await self._grants.record(
                    tenant=inv.tenant, invoice_id=inv.id, fund=fund,
                    amount_minor=grantable, reason=f"invoice:{inv.number}", expires_at=expires)
            # CONSOLIDATED invoice: one grant PER subscription (meta.grants). Each carries its own
            # sub_id as the discriminator, so the several grants under one invoice are distinct,
            # independently-idempotent promises (not collapsed onto a single invoice-keyed source).
            for g in (inv.meta or {}).get("grants", []) or []:
                amt = int(g.get("amount_minor", 0) or 0)
                if amt <= 0:
                    continue
                g_expires = None
                if self._cfg["plan_credit_grant_mode"] == "reset":
                    g_expires = float(g.get("period_end", 0) or 0) or None
                await self._grants.record(
                    tenant=inv.tenant, invoice_id=inv.id,
                    fund=g.get("fund") or self._cfg["grant_fund"], amount_minor=amt,
                    reason=f"invoice:{inv.number}:{g.get('sub_id', '')}", expires_at=g_expires,
                    discriminator=str(g.get("sub_id", "")))
        if self._events is not None:
            payload = _events.payment_payload(inv, source=source or inv.number)
            await self._events.emit(_events.INVOICE_PAID, payload)
            await self._events.emit(_events.PAYMENT_SUCCEEDED, payload)

        # AUTO-REACTIVATE: a subscription paused for non-payment reopens the instant the money lands,
        # from EVERY paid path (this is the one place all three converge), no manual reactivate. Only
        # `paused → active` — reactivate refuses a canceled or already-active subscription, so a
        # customer who left is not resumed. Idempotent: re-delivery finds it already active and the
        # allowed-from guard makes the second call a no-op. Last, so a paid invoice is fully recorded
        # before the subscription changes state.
        if self._cfg.get("auto_reactivate", True) and inv.subscription_id:
            sub = await self._subs.fetch(inv.subscription_id)
            if sub is not None and sub.status == "paused":
                await self.reactivate(inv.subscription_id)

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
                # A recalled transfer that had fully paid the invoice reverses the grant too — the
                # same seam as a card refund, so an off-gateway chargeback leaks nothing.
                await self._claw_back(inv)

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
        # The seller MUST be on the payload — an empty seller was exactly how a "tax invoice" went to
        # ZATCA with no registration number. Prefer the snapshot frozen on the invoice at issue,
        # falling back to the live profile for a payload assembled before the stamp is written.
        seller = inv.seller or self._cfg.get("seller", {}) or {}
        return {"number": inv.number, "issued_at": int(now_ts()), "currency": inv.currency,
                "vat_rate": self._cfg.get("vat_rate", "0.15"),
                "seller": {k: seller.get(k, "") for k in ("name", "vat_number", "cr_number", "address")},
                "buyer": buyer,
                "lines": [{"description": li["description"], "qty": li.get("quantity", 1),
                           "amount_minor": li["amount_minor"]} for li in inv.lines]}

    # ── which document THIS invoice is, now ─────────────────────────────────────────
    @staticmethod
    def _issued_doc_type(inv: Invoice) -> str:
        """What the invoice WAS issued as. The stamp is the truth; a legacy row with no stamp is
        inferred from whether a ZATCA e-invoice was finalized on it."""
        if inv.doc_type:
            return inv.doc_type
        if inv.einvoice and str(inv.einvoice.get("status", "")) not in ("", "skipped"):
            return "tax_invoice"
        return "receipt"

    def _profile_complete_now(self) -> bool:
        seller = self._cfg.get("seller", {}) or {}
        return tax_profile.complete(tax_driver=self._cfg.get("tax_driver"),
                                    vat_rate=self._cfg.get("vat_rate", "0"),
                                    seller_vat=seller.get("vat_number", ""),
                                    einvoicer_driver=self._cfg.get("einvoicer_driver"))

    def resolve_document(self, inv: Invoice) -> str:
        """The document to serve NOW — "tax_invoice" or "receipt".

        The invoice's ISSUED type (so a pre-switch invoice downloads exactly what it was, and a tax
        invoice stays a tax invoice after the mode is switched back), DOWNGRADED to a receipt if the
        live tax profile can no longer back a tax invoice — a seller registration emptied by a bad
        rollout. The safe failure is a receipt, never a tax invoice missing its registration number.
        """
        if self._issued_doc_type(inv) == "tax_invoice" and self._profile_complete_now():
            return "tax_invoice"
        return "receipt"

    def receipt_available(self, inv: Invoice) -> bool:
        """May a payment RECEIPT be served for this invoice? Any paid invoice that resolves to a
        receipt, was issued as one, or belongs to a `both`-mode tenant."""
        if inv.status != "paid":
            return False
        mode = str(self._cfg.get("document_mode", "receipt")).strip().lower()
        return (self.resolve_document(inv) == "receipt"
                or self._issued_doc_type(inv) == "receipt" or mode == "both")

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

    async def cycle_due_sweep(self) -> dict[str, Any]:
        """Invoice every subscription whose period has ended. The renewal engine.

        run_cycle() was built and correct, and SubscriptionStore.due() was built as "the renewal
        sweeper's queue" — but nothing called either on a schedule, so year-two revenue was never
        invoiced. This is the missing caller.

        WHY THE `<= now` FILTER MUST LIVE HERE. run_cycle invoices UNCONDITIONALLY — a caller that
        holds a sub id gets it charged. The due() filter is the only thing standing between "renew
        what is due" and "charge everyone, today". So the guard belongs at the sweep, never pushed
        out to whoever calls run_cycle.

        The pass is BOUNDED and does not abort on a bad row. A single subscription that cannot be
        invoiced — a deleted plan, a bad token — must not stop the other thousand from renewing, so
        each is tried in isolation and its failure recorded. run_cycle is idempotent per
        cycle:{sub}:{period_end}, so a re-run after a partial sweep re-charges nobody.

        `lead_seconds` lets an operator invoice a little ahead of the period end (a grace so a
        renewal is raised before access lapses); 0 keeps the exact boundary. Injected clock, never
        now() inline, so the sweep is testable and a fixed point in time.
        """
        limit = self._cfg["cycle_sweep_limit"]
        horizon = self._clock() + self._cfg.get("cycle_lead_seconds", 0)
        due = await self._subs.due(horizon, limit)
        mode = self._cfg.get("renewal_invoice_mode", "per_subscription")
        invoiced = failed = 0
        errors: list[dict[str, str]] = []

        # Decide the unit(s) to invoice per the MODE. per_subscription: one run_cycle per sub (today).
        # consolidated/hybrid: group the due subs by (tenant, gateway) into ONE invoice each; in hybrid
        # a `bill_separately` sub opts back out to its own invoice. Grouping by gateway keeps every
        # consolidated invoice chargeable by a single token. Money safety is unchanged: the per-sub
        # idempotency claim inside run_consolidated_cycle still guarantees one charge/advance per sub.
        singles: list = []
        groups: dict[tuple[str, str], list] = {}
        if mode in ("consolidated", "hybrid"):
            for sub in due:
                if mode == "hybrid" and getattr(sub, "bill_separately", False):
                    singles.append(sub)
                else:
                    key = (sub.tenant, self._group_gateway_key(sub), self._sub_currency(sub))
                    groups.setdefault(key, []).append(sub)
        else:
            singles = list(due)

        for sub in singles:
            try:
                await self.run_cycle(sub.id)
                invoiced += 1
            except Exception as exc:  # noqa: BLE001 — one unbillable sub must not stop the sweep
                failed += 1
                # The reason is kept for the run report, never swallowed: an operator needs to see
                # WHICH subscriptions are stuck, or a slow leak of unrenewed customers is invisible.
                errors.append({"subscription_id": sub.id, "error": type(exc).__name__})
        for gsubs in groups.values():
            try:
                inv = await self.run_consolidated_cycle(gsubs)
                if inv is not None:
                    invoiced += 1
            except Exception as exc:  # noqa: BLE001 — one group's failure must not stop the others
                failed += 1
                errors.append({"tenant": gsubs[0].tenant, "error": type(exc).__name__,
                               "subscriptions": ",".join(s.id for s in gsubs)})
        return {"due": len(due), "invoiced": invoiced, "failed": failed, "mode": mode,
                "capped": len(due) >= limit, "errors": errors}

    def _group_gateway_key(self, sub) -> str:
        """The gateway a consolidated invoice will charge on. In pinned mode that is the subscription's
        OWN pinned gateway (so different-gateway subs never share one invoice); in active mode every sub
        charges the one active gateway, so they group together (key "")."""
        if self._cfg.get("renewal_gateway_mode") == "pinned":
            return (getattr(sub, "payment_gateway", "") or "").strip()
        return ""

    def _sub_currency(self, sub) -> str:
        """The currency this subscription renews in — the snapshot the invoice will be cut in. Used to
        keep a consolidated invoice single-currency: money in two currencies must NEVER be summed into
        one total, so subscriptions of different currencies land in different groups."""
        return ((sub.price_snapshot or {}).get("currency") or self._cfg["currency"]).strip()

    async def run_consolidated_cycle(self, subs: list) -> Invoice | None:
        """Renew a group of due subscriptions for ONE (tenant, gateway) into a SINGLE invoice — a line
        per subscription, one charge, one pay-link. Each subscription is claimed and advanced under its
        OWN idempotency key (cycle:{sub}:{period_end}), so nothing is charged or advanced twice even if
        the group is swept concurrently or overlaps a per-sub run; a sub already renewed is excluded.
        A group-level claim stops two concurrent sweeps cutting two invoices for the same group."""
        if not subs:
            return None
        tenant = subs[0].tenant
        group_ccy = self._sub_currency(subs[0])
        bucket = int(min(float(s.current_period_end) for s in subs))
        gkey = f"consol:{tenant}:{self._group_gateway_key(subs[0])}:{group_ccy}:{bucket}"
        proceed, prior = await self._idempo.claim(gkey)
        if prior is not None:
            return await self._invoices.fetch(prior.get("invoice_id", "")) if prior.get("invoice_id") else None
        if not proceed:
            raise ValueError("CYCLE_IN_PROGRESS")
        lines: list[dict] = []
        grants_meta: list[dict] = []
        claimed: list[tuple[Any, str]] = []               # (sub, its per-sub idempo key)
        subtotal = discount = tax = total = 0
        currency = None
        rep_token = ""
        for sub in subs:
            if self._sub_currency(sub) != group_ccy:
                continue          # defence: never sum two currencies into one total (checked pre-claim)
            skey = f"cycle:{sub.id}:{int(sub.current_period_end)}"
            sproceed, sprior = await self._idempo.claim(skey)
            if sprior is not None or not sproceed:
                continue                                  # already renewed / in progress → exclude
            r = await self._compute_renewal(sub)
            currency = currency or r["currency"]
            lines.append(r["line"])
            subtotal += r["taxable"]; discount += r["discount"]; tax += r["tax"]; total += r["total"]
            sold_period_end = sub.current_period_end      # the period being sold (grant expiry basis)
            sub.current_period_start = sub.current_period_end
            sub.current_period_end += _INTERVAL.get(sub.interval, _INTERVAL["month"])
            sub.first_period_prorated = False
            if sub.status == "trialing":
                sub.status = "active"
            await self._subs.save(sub)
            if r["granted"] > 0:
                grants_meta.append({"sub_id": sub.id, "fund": self._cfg["grant_fund"],
                                    "amount_minor": r["granted"], "period_end": sold_period_end})
            rep_token = rep_token or sub.payment_token
            claimed.append((sub, skey))
        if not claimed:
            await self._idempo.mark(gkey, {"invoice_id": ""})   # all already renewed concurrently
            return None
        meta: dict[str, Any] = {"consolidated": True}
        if grants_meta:
            meta["grants"] = grants_meta
        inv = Invoice(id=new_id("inv_"), tenant=tenant,
                      number=await self._invoices.next_number(tenant), subscription_id="",
                      lines=lines, subtotal_minor=subtotal, discount_minor=discount, tax_minor=tax,
                      total_minor=total, currency=currency or self._cfg["currency"], status="open",
                      created=iso(), meta=meta)
        await self._invoices.save(inv)
        for sub, skey in claimed:
            await self._idempo.mark(skey, {"invoice_id": inv.id})
        await self._idempo.mark(gkey, {"invoice_id": inv.id})
        await self._charge_invoice(inv, rep_token, claimed[0][0])   # one charge, group's gateway/token
        if self._events is not None:
            for sub, _ in claimed:
                await self._events.emit(_events.SUBSCRIPTION_RENEWED, {
                    "tenant": tenant, "subscription_id": sub.id, "plan_id": sub.plan_id,
                    "invoice_id": inv.id, "period_key": f"cycle:{sub.id}:{int(sub.current_period_start)}",
                    "period_start": sub.current_period_start, "period_end": sub.current_period_end,
                    "limits": dict(sub.limits_snapshot or {}) if hasattr(sub, "limits_snapshot") else {}})
        return await self._invoices.fetch(inv.id) or inv

    async def dunning_sweep(self) -> dict[str, Any]:
        due = await self._payments.due(now_ts(), self._cfg["list_limit"])
        retried = 0
        for pay in due:
            inv = await self._invoices.fetch(pay.invoice_id)
            if inv is None or inv.status == "paid":
                continue
            sub = await self._subs.fetch(inv.subscription_id)
            token = sub.payment_token if sub else ""
            # A wallet-renewal sub retries through the WALLET, not the gateway — there is no "wallet"
            # payment adapter to charge, and a transient wallet outage should be re-offered to the
            # wallet. _charge_from_wallet reactivates on success and re-suspends/re-dunns on failure.
            if self._wallet_renewal_fund(sub) is not None:
                pay.status = "pending"
                if await self._charge_from_wallet(inv, pay, sub):
                    if pay.status == "succeeded" and sub and sub.status == "past_due":
                        sub.status = "active"
                        await self._subs.save(sub)
                    retried += 1
                    continue
            gw = await self._gw_for_sub(sub) if sub is not None else self._gw   # pinned: sub's gateway
            try:
                charge = gw.charge(amount_minor=inv.total_minor, currency=inv.currency,
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
                             secret: str | tuple[str, ...],
                             webhook_tenant: str = "") -> dict[str, Any]:
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
        # Tenant binding: the secret that verified belongs to `webhook_tenant`; the payment must too.
        # Prevents driving another tenant's payment with a valid-but-wrong-tenant signing secret.
        if webhook_tenant and pay.tenant != webhook_tenant:
            return {"ok": False, "reason": "TENANT_MISMATCH", "type": event.type}
        # EVENT-LEVEL DEDUPE (defence-in-depth over the state machine below). A gateway re-delivers a
        # notification, and a hostile party may replay a captured one; some gateways (field-digest
        # schemes) carry no timestamp, so a freshness window is impossible — a per-event fingerprint is
        # the correct, gateway-agnostic replay guard. Key = the gateway's own event id when present,
        # else a composite (provider:charge:type). Routed through the SAME atomic idempotency claim the
        # rest of the service uses (one mechanism), so a duplicate replays the first result and never
        # re-runs the outcome; a crash mid-process leaves a pending claim that re-runs after its lease.
        fp = getattr(event, "event_id", "") or f"{provider}:{event.provider_id}:{event.type}"
        wh_key = f"wh:{fp}"
        proceed, prior = await self._idempo.claim(wh_key)
        if prior is not None:
            return {**prior, "duplicate": True}
        if not proceed:
            # A concurrent delivery of the SAME event is mid-process; the gateway will retry.
            return {"ok": True, "reconciled": False, "in_progress": True, "type": event.type}
        outcome = await self._apply_outcome(pay, event.type)
        result = {"ok": True, "reconciled": True, "type": event.type,
                  "payment_status": outcome["payment_status"]}
        await self._idempo.mark(wh_key, result)
        return result

    async def _apply_outcome(self, pay: Payment, kind: str) -> dict[str, Any]:
        """Move a payment (and its invoice) to the outcome a gateway reports — the ONE seam a
        pushed webhook (handle_webhook) and a pulled reconcile (reconcile_payment) both funnel
        through, so the money transition is written in exactly one place.

        IDEMPOTENT by construction: each branch is guarded on the payment not already being in the
        target state, so a webhook and a later reconcile of the same event apply once between them —
        never a double mark-paid, a double dunning, or a double clawback. `kind` is the normalised
        WebhookEvent type: payment.succeeded | payment.failed | refund.
        """
        inv = await self._invoices.fetch(pay.invoice_id)
        changed = False
        if kind == "payment.succeeded" and pay.status != "succeeded":
            pay.status = "succeeded"
            pay.next_retry_at = None
            await self._payments.save(pay)
            if inv and inv.status != "paid":
                await self._mark_paid(inv)
            changed = True
        elif kind == "payment.failed" and pay.status != "failed":
            pay.status = "failed"
            await self._dunning(pay)
            await self._payments.save(pay)
            changed = True
        elif kind == "refund" and pay.status != "refunded":
            pay.status = "refunded"
            await self._payments.save(pay)
            if inv:
                # A gateway refund webhook carries no partial amount here, so it is a FULL refund:
                # record the whole total as returned and void the invoice.
                inv.refunded_minor = inv.total_minor
                inv.status = "void"
                inv.voided_at = iso()           # WHEN the money was fully returned — for dispute timelines
                await self._invoices.save(inv)
                await self._claw_back(inv)      # same reversal as a manual refund / a settlement recall
            changed = True
        return {"changed": changed, "payment_status": pay.status}

    async def reconcile_payment(self, payment_id: str) -> dict[str, Any]:
        """PULL a payment's current status from the gateway and apply it — the recovery path for a
        webhook that never arrived or failed signature verification (an invoice left unpaid while the
        customer's money already moved). It drives the SAME idempotent `_apply_outcome` seam as the
        webhook, so a reconcile after a delayed webhook, or a webhook after a reconcile, is safe.

        Never invents a status: a provider without a status endpoint is reported `unsupported`, and a
        gateway that is unreachable is reported as such — the reconcile does nothing rather than
        guessing, because guessing "paid" here would mark an unpaid invoice settled.
        """
        pay = await self._payments.fetch(payment_id)
        if pay is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        fetch = getattr(self._gw, "fetch_status", None)
        if fetch is None:
            return {"ok": False, "reason": "UNSUPPORTED",
                    "provider": getattr(self._gw, "driver", "")}
        if not pay.provider_id:
            return {"ok": False, "reason": "NO_PROVIDER_ID", "payment_status": pay.status}
        try:
            charge = fetch(provider_id=pay.provider_id, currency=pay.currency)
        except GatewayUnavailable as exc:
            return {"ok": False, "reason": "GATEWAY_UNAVAILABLE", "detail": str(exc)}
        kind = {"succeeded": "payment.succeeded", "failed": "payment.failed",
                "refunded": "refund"}.get(charge.status, "")
        if not kind:
            # The provider still shows it pending/unknown — nothing definitive to apply.
            return {"ok": True, "reconciled": False, "provider_status": charge.status,
                    "payment_status": pay.status}
        outcome = await self._apply_outcome(pay, kind)
        return {"ok": True, "reconciled": outcome["changed"], "provider_status": charge.status,
                "payment_status": outcome["payment_status"]}

    async def _claw_back(self, inv: Invoice, *, partial: bool = False) -> None:
        """Reverse the wallet grant an invoice made, when that invoice is refunded or charged back.

        The mirror of the grant in `_mark_paid`: ONE seam every reversal path funnels into, so a
        chargeback settled off-gateway reverses the same credits a card refund does. It is a set of
        operator CHOICES — the whole thing can be off, negative balances allowed or not, partials
        reversed or not, expired grants skipped or not — because how a business handles a refunded
        credit is a policy, not a law.

        Fail-safe: it records a DURABLE clawback row (delivered by the outbox drain) and never
        blocks the refund on the wallet. A no-op when no grant was made, no wallet is wired, or the
        operator disabled it.
        """
        if self._grants is None or not self._cfg.get("clawback_enabled", True):
            return
        if partial and not self._cfg.get("clawback_on_partial", False):
            return
        grantable = int((inv.meta or {}).get("grant_minor", 0) or 0)
        if grantable <= 0:
            return
        # An expired `reset` grant already lapsed in the wallet; reversing it would drive the
        # balance negative for credits the customer never effectively held. Skip by default.
        if (self._cfg.get("clawback_skip_expired", True)
                and self._cfg.get("plan_credit_grant_mode") == "reset"):
            period_end = float((inv.meta or {}).get("period_end", 0) or 0)
            if period_end and period_end <= now_ts():
                return
        await self._grants.record_clawback(
            tenant=inv.tenant, invoice_id=inv.id, fund=self._cfg["grant_fund"],
            amount_minor=grantable, reason=f"refund:{inv.number}",
            allow_negative=bool(self._cfg.get("clawback_allow_negative", True)))

    async def refund(self, payment_id: str, amount_minor: int | None = None) -> dict[str, Any]:
        pay = await self._payments.fetch(payment_id)
        if pay is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        gw = await self._gw_for_refund(pay)     # pinned: refund via the gateway that CHARGED it
        charge = gw.refund(provider_id=pay.provider_id, amount_minor=amount_minor,
                           currency=pay.currency)
        if charge.ok:
            pay.status = "refunded"
            await self._payments.save(pay)
            inv = await self._invoices.fetch(pay.invoice_id)
            if inv:
                refund_amt = amount_minor if amount_minor is not None else inv.total_minor
                is_partial = amount_minor is not None and amount_minor < inv.total_minor
                # Record the amount that went BACK, so "what the shop kept paying" is answerable
                # (total_minor - refunded_minor). A PARTIAL refund no longer voids the whole invoice —
                # the shop kept the rest, and voiding it charged commission on nothing or excluded a
                # still-paid invoice; only a full refund (refunded reaching the total) voids it.
                inv.refunded_minor = (inv.refunded_minor or 0) + refund_amt
                inv.credit_note = True
                if inv.refunded_minor >= inv.total_minor:
                    inv.status = "void"
                    inv.voided_at = iso()       # WHEN it fully voided — for dispute timelines
                await self._invoices.save(inv)
                await self._ledger.record(tenant=inv.tenant, kind="refund",
                                          amount_minor=-refund_amt,
                                          currency=inv.currency, invoice_id=inv.id, source="refund")
                # Reverse the wallet grant this invoice made — else the customer keeps the credits
                # the refunded money bought. A partial refund reverses only if the operator opted in.
                await self._claw_back(inv, partial=is_partial)
                if self._events is not None:
                    # A consumer that granted something on payment.succeeded needs to know to take
                    # it back. Without this, a refunded customer keeps whatever the payment bought.
                    await self._events.emit(_events.PAYMENT_REFUNDED, {
                        **_events.payment_payload(inv, payment_id=pay.id, source="refund"),
                        "amount_minor": amount_minor or inv.total_minor,
                    })
        return {"ok": charge.ok, "status": charge.status}

    async def prepare_checkout(self, *, amount_minor: int, currency: str = "", reference: str = "",
                               return_url: str = "", metadata: dict | None = None) -> dict[str, Any]:
        """Server-create a checkout/session for the customer's widget or redirect — so the app never
        proxies a provider key. Delegates to the gateway; a provider with no server-side session
        SAYS so (`supported: False`) rather than faking one. Completion still reconciles through the
        webhook / GET-status path into _mark_paid, so a completed checkout is the same paid invoice
        as a direct charge — there is no second money path.
        """
        fn = getattr(self._gw, "prepare_checkout", None)
        if fn is None:
            return {"supported": False, "ok": False, "provider": getattr(self._gw, "driver", ""),
                    "error": "this provider has no server-side checkout"}
        session = fn(amount_minor=int(amount_minor), currency=currency or self._cfg["currency"],
                     reference=reference, return_url=return_url, metadata=metadata or {})
        return {"supported": True, **session.to_dict()}

    async def create_invoice_pay_link(self, *, tenant: str, invoice_id: str, return_url: str = "",
                                      ttl_seconds: int = 0) -> dict[str, Any]:
        """A trackable hosted-checkout link for an EXISTING unpaid invoice — to send to the payer
        (e.g. over WhatsApp). The amount is the INVOICE's total (server-authoritative, never a caller
        field); the link is opened on the tenant's/platform's own gateway; completion flows through
        the SAME webhook / reconcile -> _apply_outcome -> _mark_paid seam as any other payment, so the
        SAME invoice is marked paid exactly once. A high-entropy token + TTL back the public
        redirector (so the link can ride inside a Meta template's fixed-base URL button)."""
        row = await self._invoices.get(invoice_id)
        inv = Invoice.from_dict(row) if row else None
        # IDOR guard: an invoice that is not this tenant's is indistinguishable from a missing one.
        if inv is None or inv.tenant != tenant:
            raise ValueError("INVOICE_NOT_FOUND")
        if inv.status in ("paid", "void", "uncollectible"):
            raise ValueError(f"INVOICE_NOT_PAYABLE:{inv.status}")
        # SINGLE-USE per invoice: if a non-expired pending link already exists, RETURN IT instead of
        # opening a second gateway session — a double POST (retry/double-click) or a second recipient
        # must never yield two live checkouts for one invoice (a double-charge). Industry default
        # (Adyen/Stripe pay-links are single-use).
        if self._paylinks is not None:
            live = await self._paylinks.live_for_invoice(inv.id, self._clock())
            if live is not None:
                return {"invoice_id": inv.id, "payment_id": live.get("payment_id", ""),
                        "amount_minor": int(inv.total_minor), "currency": inv.currency,
                        "checkout_url": str(live.get("checkout_url", "") or ""),
                        "token": str(live.get("token", "") or ""),
                        "expires_at": float(live.get("expires_at", 0) or 0), "status": "pending",
                        "reused": True, "checkout": {"supported": True}}
        fn = getattr(self._gw, "prepare_checkout", None)
        if fn is None:
            raise UpgradeChargeFailed("CHECKOUT_UNSUPPORTED")
        try:
            session = fn(amount_minor=int(inv.total_minor), currency=inv.currency,
                         reference=inv.id, return_url=return_url,
                         metadata={"tenant": tenant, "invoice_id": inv.id})
        except GatewayUnavailable as ex:
            raise UpgradeChargeUnavailable(str(ex)) from ex
        if not getattr(session, "ok", False):
            raise UpgradeChargeFailed(getattr(session, "error", "") or "CHECKOUT_FAILED")
        checkout_url = str(getattr(session, "redirect_url", "") or "")
        # A pending payment tied to THIS invoice, keyed by the gateway session so webhook/reconcile
        # resolves it (PaymentStore.by_provider_id) and _mark_paid settles the invoice.
        pay = Payment(id=new_id("pay_"), tenant=tenant, invoice_id=inv.id,
                      amount_minor=int(inv.total_minor), currency=inv.currency,
                      provider=getattr(session, "provider", ""), provider_id=session.id,
                      status="pending", created=iso())
        await self._payments.save(pay)
        ttl = int(ttl_seconds or self._cfg.get("pay_link_ttl_seconds", 259_200))
        expires_at = self._clock() + ttl
        token = ""
        if self._paylinks is not None and checkout_url:
            token = _secrets.token_urlsafe(32)          # unguessable; the redirector's only key
            try:
                await self._paylinks.save({
                    "token": token, "tenant": tenant, "payment_id": pay.id, "invoice_id": inv.id,
                    "checkout_url": checkout_url, "expires_at": expires_at, "status": "pending"})
            except Exception:
                # No reachable link → do not leave an orphan pending payment for the reconcile poller.
                await self._payments.delete(pay.id)
                raise
        return {"invoice_id": inv.id, "payment_id": pay.id, "amount_minor": int(inv.total_minor),
                "currency": inv.currency, "checkout_url": checkout_url, "token": token,
                "expires_at": expires_at, "status": "pending",
                "checkout": {"supported": True, **session.to_dict()}}

    async def resolve_pay_link(self, token: str) -> dict[str, Any] | None:
        """The public redirector's lookup: token -> the hosted checkout_url, or None (unknown), or
        {"expired": True} once past its TTL. Holds no secret and no card data — only a URL we minted."""
        if self._paylinks is None or not token:
            return None
        row = await self._paylinks.by_token(token)
        if not row:
            return None
        if self._clock() > float(row.get("expires_at", 0) or 0):
            return {"expired": True}
        # STATUS-AWARE: a link must stop working once its invoice is no longer open — otherwise a payer
        # could pay an already-paid (or voided) invoice through a still-"pending" link. Re-fetch live.
        inv_row = await self._invoices.get(str(row.get("invoice_id", "") or ""))
        if inv_row is not None and str(inv_row.get("status", "")) != "open":
            return {"settled": True, "status": str(inv_row.get("status", ""))}
        return {"checkout_url": str(row.get("checkout_url", "") or ""),
                "invoice_id": row.get("invoice_id", ""), "payment_id": row.get("payment_id", "")}

    async def wallet_topup_checkout(self, *, tenant: str, amount_minor: int, fund: str = "",
                                    currency: str = "", return_url: str = "") -> dict[str, Any]:
        """Start a HOSTED-CHECKOUT wallet top-up — for a gateway with no card-on-file/token (e.g.
        TuwaiqPay: bill -> payment link -> customer pays -> poll/verify). The token path
        (wallet_topup) cannot serve these; this one raises a PENDING invoice + payment carrying the
        SAME grant meta, so when the payment completes — via a webhook OR a reconcile poll — the
        existing _apply_outcome -> _mark_paid seam credits the wallet fund EXACTLY as the token path
        does. One money path, one reconciliation, one idempotency guard.

        The invoice/payment are persisted only after the gateway session is created (a gateway that
        cannot open one leaves no orphan invoice). The payment's provider_id is the session id, so a
        later reconcile_payment(payment_id) polls the right bill."""
        amount = int(amount_minor)
        if amount <= 0:
            raise ValueError("AMOUNT_MUST_BE_POSITIVE")
        cap = int(self._cfg.get("wallet_topup_max_minor", 0) or 0)
        if cap and amount > cap:
            raise ValueError("AMOUNT_EXCEEDS_MAX")
        fund = (fund or self._cfg.get("wallet_topup_fund", "") or "").strip()
        if not fund:
            raise ValueError("FUND_REQUIRED")
        currency = currency or self._cfg["currency"]
        fn = getattr(self._gw, "prepare_checkout", None)
        if fn is None:
            raise UpgradeChargeFailed("CHECKOUT_UNSUPPORTED")
        if self._cfg.get("wallet_topup_tax_mode", "receipt") == "tax_invoice":
            taxable, tax, total = self._split_tax(amount, includes_tax=False)
            force_doc = None
        else:
            taxable, tax, total = amount, 0, amount
            force_doc = "receipt"
        inv_id = new_id("inv_")                              # the checkout reference, before we persist
        try:
            session = fn(amount_minor=total, currency=currency, reference=inv_id,
                         return_url=return_url, metadata={"topup_fund": fund, "tenant": tenant})
        except GatewayUnavailable as ex:
            raise UpgradeChargeUnavailable(str(ex)) from ex
        if not getattr(session, "ok", False):
            # The provider declined to open a session (or has none): nothing persisted, no orphan.
            raise UpgradeChargeFailed(getattr(session, "error", "") or "CHECKOUT_FAILED")
        meta: dict[str, Any] = {"grant_minor": total, "grant_fund": fund, "topup": True}
        if force_doc:
            meta["force_doc_type"] = force_doc
        inv = Invoice(id=inv_id, tenant=tenant, number=await self._invoices.next_number(tenant),
                      subscription_id="", lines=[{"description": "Wallet top-up", "quantity": 1,
                                                  "unit_minor": taxable, "amount_minor": taxable}],
                      subtotal_minor=taxable, tax_minor=tax, total_minor=total, currency=currency,
                      status="open", created=iso(), meta=meta)
        await self._invoices.save(inv)
        pay = Payment(id=new_id("pay_"), tenant=tenant, invoice_id=inv.id, amount_minor=total,
                      currency=currency, provider=getattr(session, "provider", ""),
                      provider_id=session.id, status="pending", created=iso())
        await self._payments.save(pay)
        return {"invoice_id": inv.id, "invoice_number": inv.number, "payment_id": pay.id,
                "amount_minor": total, "fund": fund, "currency": currency, "doc_type": force_doc or "",
                "status": "pending", "checkout": {"supported": True, **session.to_dict()}}

    async def list_pending_topups(self, *, limit: int = 50, older_than_seconds: int = 0,
                                  after: str | None = None) -> dict[str, Any]:
        """PENDING wallet-topup payments awaiting a reconcile poll — the queue an operations poller
        walks, then calls reconcile_payment per row (hosted-checkout gateways have no signed webhook).

        Only top-up payments are returned (the invoice's meta.topup), never ordinary pending charges.
        `older_than_seconds` skips payments opened moments ago, giving the gateway time before the
        first poll. Bounded and paged: scans at most a few pages, returns a cursor to continue."""
        lim = max(1, min(int(limit), 200))
        skip_newer_than = None
        if older_than_seconds and older_than_seconds > 0:
            skip_newer_than = self._clock() - int(older_than_seconds)
        out: list[dict[str, Any]] = []
        cursor = after
        pages = 0
        while len(out) < lim and pages < 20:
            pages += 1
            rows, cursor = await self._payments.pending(limit=100, after=cursor)
            for r in rows:
                if skip_newer_than is not None and _epoch(r.get("created", "")) > skip_newer_than:
                    continue
                inv = await self._invoices.fetch(r.get("invoice_id", ""))
                if inv is None or not (inv.meta or {}).get("topup"):
                    continue
                out.append({"payment_id": r["id"], "tenant": r.get("tenant", ""),
                            "amount_minor": r.get("amount_minor", 0),
                            "fund": (inv.meta or {}).get("grant_fund", ""),
                            "currency": r.get("currency", ""), "created": r.get("created", ""),
                            "provider_id": r.get("provider_id", "")})
                if len(out) >= lim:
                    break
            if not cursor:
                break
        return {"payments": out, "next_cursor": cursor}

    async def render_pdf(self, inv: Invoice) -> tuple[str, bytes] | None:
        # BILL TO must be a real party, not a database key. The buyer's legal identity lives on the
        # subscription (name / VAT / CR), not on the invoice, so resolve it here rather than let the
        # renderer fall back to printing the raw tenant id as if it were a name. A B2C invoice with
        # no named buyer carries the tenant only as an ACCOUNT reference — honest, and ZATCA does not
        # require a buyer name on a simplified invoice.
        data = await self._document_data(inv)
        return self._pdf.render(data)

    async def _document_data(self, inv: Invoice) -> dict[str, Any]:
        """The invoice dict a document renders from, with a real BILL-TO party resolved."""
        data = inv.to_dict()
        buyer = await self._buyer_for(inv)
        name = (buyer.get("name") or "").strip()
        if name:
            data["buyer"] = {k: v for k, v in buyer.items()
                             if k in ("name", "vat_number", "cr_number", "address") and v}
        else:
            data["buyer"] = {"account": inv.tenant}
        # The seller printed on the document is the one FROZEN at issue for a tax invoice, so a
        # delivered document keeps its original registration number even if the live value later
        # changes; a receipt (or a legacy row) falls back to the live seller identity.
        if inv.seller:
            data["seller"] = inv.seller
        return data

    async def render_receipt(self, inv: Invoice) -> tuple[str, bytes] | None:
        """The non-tax payment RECEIPT for a PAID invoice — carries the gateway's transaction
        reference (or, for an off-gateway settlement, its method + bank reference). Returns None if
        the invoice is not paid or no receipt renderer is wired."""
        if inv.status != "paid":
            return None
        fn = getattr(self._pdf, "render_receipt", None)
        if fn is None:
            return None
        data = await self._document_data(inv)
        pay = await self._payments.for_invoice(inv.id)
        if pay is not None:
            ref = {"provider": pay.provider, "provider_id": pay.provider_id, "paid_at": inv.paid_at}
        else:
            # Paid off-gateway: show the settlement's method + reference, never a blank.
            stls = await self._settlements.for_invoice(inv.id, 1)
            stl = stls[0] if stls else {}
            ref = {"method": stl.get("method", ""), "reference": stl.get("reference", ""),
                   "paid_at": inv.paid_at}
        return fn(data, ref)
