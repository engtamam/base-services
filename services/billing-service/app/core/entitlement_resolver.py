"""Resolving what a tenant may do, from everything they pay for.

The old resolver read ONE subscription. This reads every billable one and merges them, which makes
four ordinary billing shapes expressible that were not:

    per-item catalogue   one subscription per product — the union IS the entitlement
    add-ons              a subscription granting only a delta, no base plan
    prepaid packs        a non-renewing purchase (interval "once") that tops up a counter
    contract terms       a fixed-term negotiated cap with no recurring plan

The last three need NO new model. A pack is a subscription that does not renew; a contract is one
with an agreed period and its own limits_snapshot. Once resolution unions rather than picks, they
fall out of the same code path — which is the argument for fixing the resolver rather than adding
three special cases.

WHICH SUBSCRIPTIONS COUNT is a credit decision, not a technical one.

`active` and `trialing` obviously. `past_due` is the interesting one: a late bank transfer must not
switch off a hospital's cameras, and a customer who stopped paying two months ago is not entitled.
So past_due contributes only INSIDE the existing grace window, and the window is config, because a
platform's tolerance for unpaid usage is theirs to set and not ours to assume.

`canceled` contributes nothing, ever. That one is not configurable: a cancelled subscription that
still granted access would make cancellation a suggestion.

AND IT FAILS CLOSED ON A LAPSED PERIOD. Status is not enough: an `active` subscription whose paid
period has ended keeps contributing until something changes its status, and the only thing that does
is the renewal sweep. So if the sweep is paused, throws, or a deployment gap swallows a day, every
affected tenant silently keeps full access — nothing red, no probe failing, the only evidence an
invoice never raised that nobody reads until month end. A missed sweep must look like an outage, not
generosity. So a subscription whose `current_period_end` has passed (beyond a small, stated
tolerance — CYCLE_LEAD_SECONDS already renews AHEAD of the boundary) stops contributing, and the
drop is surfaced (`lapsed`, and a log a spike in is alertable). A row with no period end is a
non-renewing pack or open-ended contract and has no period to lapse. This is also what makes manual
(non-auto) renewal work at all: a lapsed subscription stops on its own, with no cancel-sweeper to
write.

GRANDFATHERING IS PER SUBSCRIPTION, BEFORE THE MERGE. Each row's signup terms win for that row.
Merging first and grandfathering after would apply one subscription's historic protection to
another bought yesterday — the customer would get terms they never agreed to, in whichever
direction happened to be wrong.
"""
from __future__ import annotations

from typing import Any

from app.core import entitlement_merge as _merge
from app.observability.logging import get_logger

_log = get_logger("app.entitlements")

ACTIVE, TRIALING, PAST_DUE, CANCELED = "active", "trialing", "past_due", "canceled"

# Stacking. An add-on ADDS to what a tenant already has; a `replace` plan supersedes.
STACK, REPLACE_PLAN = "stack", "replace"


class EntitlementResolver:
    # Every setting this resolver reads. Declared here and required — NOT defaulted at the call
    # site with `.get(key, fallback)`, because an inline fallback is a second place a default
    # lives, and the two drift the day someone changes config.py and not this file. A missing key
    # is a KeyError at construction, which is a deploy that fails rather than a tenant resolved
    # against a number nobody chose.
    REQUIRED_CFG = ("billable_statuses", "grace_days", "grandfather", "max_subscriptions",
                    "merge_strategies", "rank_orders", "period_tolerance_seconds")

    def __init__(self, *, subs, plans, cfg) -> None:
        missing = [k for k in self.REQUIRED_CFG if k not in cfg]
        if missing:
            raise KeyError(f"EntitlementResolver is missing config: {', '.join(missing)}")
        self._subs = subs
        self._plans = plans
        self._cfg = cfg

    # ── which subscriptions may contribute ────────────────────────────────────
    def _statuses(self) -> tuple[str, ...]:
        return tuple(self._cfg["billable_statuses"])

    def _period_current(self, sub: dict, status: str, now: float) -> bool:
        """Whether this subscription's PAID PERIOD still covers `now` — the check that makes the
        resolver fail CLOSED.

        The renewal sweep, not the resolver, moves a period forward (CYCLE_LEAD_SECONDS invoices
        before the boundary so a healthy sweep renews ahead of it). So if the resolver ignores the
        period end, a paused or erroring sweep silently keeps granting full access — the same class
        as a terminal state read as a self-resolving one. Reading the field the row already carries
        makes a missed sweep look like a lapse instead.

          past_due — the credit-decision window: entitled only while measurably, boundedly overdue,
            inside GRACE_DAYS. A missing period end, or a zero/negative grace, is OUT — an
            unmeasurable overdue is not a short one, and the other default grants unlimited free
            usage to any row with a missing field.
          active / trialing (and any other billable status) — the period must not have ended beyond
            a small tolerance (a sweep running a little late must not cut anyone off). A row with NO
            period end is a non-renewing pack or an open-ended contract: no period to lapse, so it
            keeps counting.
        """
        period_end = float(sub.get("current_period_end") or 0)
        if status == PAST_DUE:
            grace_days = float(self._cfg["grace_days"])
            if grace_days <= 0 or not period_end:
                return False
            return now < period_end + grace_days * 86400
        if not period_end:
            return True
        tolerance = max(0.0, float(self._cfg["period_tolerance_seconds"]))
        return now <= period_end + tolerance

    def _keep_reason(self, sub: dict, now: float) -> tuple[bool, str]:
        """(kept, reason). The reason is why a row was dropped — so a lapse is distinguishable from a
        wrong status, and can be surfaced instead of silently vanishing."""
        status = sub.get("status", "")
        if status == CANCELED:
            return False, "canceled"          # never, and deliberately not configurable
        if status not in self._statuses():
            return False, "status_not_billable"
        if not self._period_current(sub, status, now):
            return False, "period_lapsed"
        return True, status

    def contributing(self, rows: list[dict], now: float) -> list[dict]:
        """The subscriptions whose limits count right now."""
        return [sub for sub in rows if self._keep_reason(sub, now)[0]]

    # ── limits of each subscription ───────────────────────────────────────────
    async def _limits_for_all(self, live: list[dict]) -> list[tuple[str, dict[str, Any]]]:
        """Each subscription's limits, grandfathered PER ROW, in one bounded pass.

        A row that carries its signup snapshot needs no plan at all — the normal case, and free.
        Only rows WITHOUT one need the catalogue, and those are fetched ONCE PER DISTINCT PLAN
        rather than once per subscription: ten seats bought on the same plan is one read, not ten.

        This is the hot path — feature-flag-service resolves per flag evaluation — so unioning N
        subscriptions must not turn one cached read into N uncached ones. Without the dedup this
        would be an N+1 that only appears for tenants who have grandfathering off, which is
        exactly the kind of cost that ships because the common case hides it.
        """
        needed = {sub.get("plan_id", "") for sub in live
                  if not (self._cfg["grandfather"] and sub.get("limits_snapshot"))}
        plans: dict[str, Any] = {}
        for plan_id in sorted(p for p in needed if p):
            plan = await self._plans.fetch(plan_id)
            plans[plan_id] = dict(plan.limits) if plan else {}

        out: list[tuple[str, dict[str, Any]]] = []
        for sub in live:
            if self._cfg["grandfather"] and sub.get("limits_snapshot"):
                out.append((sub.get("id", ""), dict(sub["limits_snapshot"])))
            else:
                out.append((sub.get("id", ""), dict(plans.get(sub.get("plan_id", ""), {}))))
        return out

    # ── the merge ─────────────────────────────────────────────────────────────
    async def resolve(self, tenant: str, now: float) -> dict[str, Any]:
        """Every limit this tenant holds, with provenance for each.

        Returns `{"limits": {key: value}, "detail": {key: {value, strategy, from}}, ...}`. The flat
        `limits` map is what existing callers read, unchanged in shape; `detail` is the part that
        makes an answer arguable — "I paid for 25, why do I have 20?" is not answerable from a
        number alone.
        """
        rows = await self._subs.billable_for(tenant, self._statuses(),
                                             limit=self._cfg["max_subscriptions"])
        live = self.contributing(rows, now)
        # Dropped specifically because the paid period ended — as opposed to a wrong status. Kept
        # separate from `live` so the lapse can be surfaced instead of vanishing silently. A second
        # pass over a bounded list (max_subscriptions) — clarity over a micro-optimisation.
        lapsed = [s for s in rows if self._keep_reason(s, now)[1] == "period_lapsed"]

        # A lapse means the renewal sweep did not move this period forward. In a healthy system
        # CYCLE_LEAD_SECONDS invoices ahead of the boundary, so an active/trialing lapse should never
        # happen — several at once is the sweep being paused or erroring, and that must be visible NOW
        # (a spike in this log is alertable) rather than surfacing at month-end reconciliation as an
        # invoice that was never raised. A past_due lapse is the ordinary end of dunning, not an
        # anomaly, so it is logged at info.
        for sub in lapsed:
            period_end = float(sub.get("current_period_end") or 0)
            fields = {"tenant": tenant, "subscription": sub.get("id", ""),
                      "status": sub.get("status", ""), "current_period_end": period_end,
                      "overdue_seconds": round(now - period_end, 3) if period_end else None}
            if sub.get("status") == PAST_DUE:
                _log.info("entitlement_dunning_lapsed", extra=fields)
            else:
                _log.warning("entitlement_period_lapsed", extra=fields)

        # A `replace` plan supersedes everything before it. Applied by TRUNCATING the list rather
        # than by merging and then overwriting, so a superseded subscription cannot leak a key the
        # replacing plan does not mention.
        for i in range(len(live) - 1, -1, -1):
            if (live[i].get("stacking") or STACK) == REPLACE_PLAN:
                live = live[i:]
                break

        per_sub = await self._limits_for_all(live)

        contributions: dict[str, list[dict]] = {}
        for sub_id, limits in per_sub:
            for key, value in limits.items():
                contributions.setdefault(key, []).append(
                    {"subscription": sub_id, "value": value})

        declared = self._cfg["merge_strategies"]
        ranks = self._cfg["rank_orders"]
        limits: dict[str, Any] = {}
        detail: dict[str, Any] = {}
        refused: dict[str, str] = {}
        for key, parts in contributions.items():
            try:
                answer = _merge.merge(key, parts, declared.get(key),
                                      tuple(ranks.get(key) or ()))
            except _merge.MergeRefused as exc:
                # A key we cannot merge is a key we do not grant. Recorded rather than dropped:
                # an entitlement missing for a reason nobody can see is the original bug again.
                refused[key] = str(exc)
                continue
            limits[key] = answer["value"]
            detail[key] = answer

        anchor = min((float(s.get("current_period_start") or 0) for s in live), default=0.0)
        return {
            "limits": limits,
            "detail": detail,
            "refused": refused,
            "anchor": anchor or None,
            # Kept for callers that recorded WHICH subscription answered. With several, the
            # oldest is the anchor — but `detail` is the honest answer and this is compatibility.
            "subscription_id": live[0].get("id", "") if live else "",
            "subscriptions": [s.get("id", "") for s in live],
            # Subscriptions dropped because their paid period ended — the honest, visible answer to
            # "why did access stop / why is nobody being invoiced?": the renewal did not run. Empty
            # in a healthy system.
            "lapsed": [s.get("id", "") for s in lapsed],
        }
