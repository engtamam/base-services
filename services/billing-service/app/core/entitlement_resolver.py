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

GRANDFATHERING IS PER SUBSCRIPTION, BEFORE THE MERGE. Each row's signup terms win for that row.
Merging first and grandfathering after would apply one subscription's historic protection to
another bought yesterday — the customer would get terms they never agreed to, in whichever
direction happened to be wrong.
"""
from __future__ import annotations

from typing import Any

from app.core import entitlement_merge as _merge

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
                    "merge_strategies", "rank_orders")

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

    def _in_grace(self, sub: dict, now: float) -> bool:
        """A past_due subscription still inside the grace window.

        Measured from the end of the period it failed to pay for, not from `now` minus a guess:
        the question is how long this customer has been overdue, and only the period end says that.
        A missing period end is treated as OUT of grace — an unmeasurable overdue is not a short
        one, and defaulting the other way would grant unlimited free usage to any row with a
        missing field.
        """
        grace_days = float(self._cfg["grace_days"])
        if grace_days <= 0:
            return False
        period_end = float(sub.get("current_period_end") or 0)
        if not period_end:
            return False
        return now < period_end + grace_days * 86400

    def contributing(self, rows: list[dict], now: float) -> list[dict]:
        """The subscriptions whose limits count right now, with the reason each was kept."""
        keep = []
        allowed = self._statuses()
        for sub in rows:
            status = sub.get("status", "")
            if status == CANCELED:
                continue                      # never, and deliberately not configurable
            if status not in allowed:
                continue
            if status == PAST_DUE and not self._in_grace(sub, now):
                continue
            keep.append(sub)
        return keep

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
        }
