"""Usage metering + real entitlement enforcement.

Before this, `check(limits, key, current)` took `current` from its CALLER — so a tenant could
state its own usage and always be allowed, and services that passed nothing got `0` and were
always allowed. The limits were declared and unenforced. This module owns the number.

Three operations, and the difference between them is the whole design:

  * `consume(key, amount)`  — a COUNTER. Atomic check-and-increment; the only call that
    enforces. Idempotent by key, because our event bus is at-least-once.
  * `release(key, amount)`  — give a counter unit back when the action failed. Never refused.
  * `set_usage(key, n)`     — a GAUGE, reported by whoever owns the thing (identity reports
    seats). Absolute, not additive: increment a gauge and deleting a user never returns the
    seat.

`check()` is advisory FOREVER. Between a check that said yes and the action, another request
can take the last unit. `if check(): act()` is a race by construction — only `consume()` is
safe, so the API makes the safe call the easy one.

Counters are keyed by their period (`…:2026-07`), so a new period is a new key and there is
no rollover job to forget. A reset cron that silently fails hands every tenant a free month.
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.core import entitlements as _ent
from app.core import periods as _periods

_COUNTERS = "usage_counters"      # {tenant}|{key}|{period}  ->  {"n": int}
_IDEMPO = "usage_idempo"          # consume idempotency window


class UsageService:
    def __init__(self, *, store, limits_resolver, clock=None, config: dict[str, Any] | None = None,
                 meter=None) -> None:
        self._store = store                      # the raw DataStore (needs incr_if_below)
        self._limits = limits_resolver           # async (tenant) -> {limits, anchor, subscription_id}
        self._clock = clock
        self._cfg = config or {}
        # The metered-usage store that `run_overage` aggregates from. NOT the money ledger:
        # the ledger's balance is sum(amount_minor), so writing *units* into it would corrupt
        # every tenant's balance.
        self._meter = meter

    def _now(self) -> float:
        return self._clock.now() if self._clock else _time.time()

    @staticmethod
    def counter_key(tenant: str, key: str, period_key: str) -> str:
        return f"{tenant}|{key}|{period_key}"

    @staticmethod
    def _idempo_key(tenant: str, idempotency_key: str) -> str:
        """Scope the consume-idempotency record to the TENANT.

        The counter row is already tenant-scoped (counter_key); the dedupe record was NOT — it was
        keyed on the caller's idempotency_key alone. Two tenants presenting the same key (an order
        number, an invoice id) collided: the second was told allowed=true with the FIRST tenant's
        counters while NOTHING was metered for it, so its cap never bound and its metered usage —
        real money, e.g. a WhatsApp send bought from Meta — went unbilled. The key is CALLER-SUPPLIED,
        so the tenant is length-prefixed: a key containing the '|' delimiter cannot be crafted to
        land in another tenant's slot (len('a')+'a|b|c' ≠ len('a|b')+'a|b|c')."""
        return f"{len(tenant)}:{tenant}|{idempotency_key}"

    async def _spec_for(self, tenant: str, key: str) -> tuple[dict, str, dict]:
        resolved = await self._limits(tenant)
        default = self._cfg.get("unknown_key_default", 0)      # deny-by-default
        sp = _ent.spec(resolved["limits"], key, default=default,
                       default_period=self._cfg.get("default_period", _ent.DEFAULT_PERIOD))
        pkey = _periods.period_key(sp["period"], self._now(), resolved.get("anchor"))
        return sp, pkey, resolved

    async def current(self, tenant: str, key: str) -> int:
        sp, pkey, _ = await self._spec_for(tenant, key)
        row = await self._store.get(_COUNTERS, self.counter_key(tenant, key, pkey))
        return int((row or {}).get("n", 0))

    # ── advisory ────────────────────────────────────────────────────────────────
    async def check(self, *, tenant: str, key: str, amount: int = 1) -> dict:
        """Read-only. Never enforces — see the module docstring."""
        try:
            sp, pkey, _ = await self._spec_for(tenant, key)
        except _ent.EntitlementError as exc:
            return {"allowed": False, "key": key, "error": str(exc), "advisory": True}
        row = await self._store.get(_COUNTERS, self.counter_key(tenant, key, pkey))
        current = int((row or {}).get("n", 0))
        out = _ent.evaluate(sp, current, amount)
        return {**out, "period": pkey, "advisory": True}

    # ── enforcing ───────────────────────────────────────────────────────────────
    async def consume(self, *, tenant: str, key: str, amount: int = 1,
                      idempotency_key: str | None = None) -> dict:
        """Atomic check-and-increment. THE call that enforces a counter limit."""
        if int(amount) < 0:
            return {"allowed": False, "key": key, "error": "NEGATIVE_AMOUNT"}
        try:
            sp, pkey, resolved = await self._spec_for(tenant, key)
        except _ent.EntitlementError as exc:
            return {"allowed": False, "key": key, "error": str(exc)}      # fail closed

        if not sp["present"]:
            # The plan never mentions this key (often: the tenant has no subscription).
            # Deny — but say why. It is not a zero-limit gauge.
            return {"allowed": False, "key": key, "error": "NOT_ENTITLED",
                    "current": 0, "remaining": 0, "limit": 0, "overage": 0}

        if sp["kind"] == "gauge":
            return {"allowed": False, "key": key, "error": "GAUGE_NOT_CONSUMABLE",
                    "hint": "report a gauge with set_usage(); incrementing it never gives the unit back"}
        if sp["kind"] == "boolean":
            out = _ent.evaluate(sp, 0, 0)
            return {**out, "period": pkey, "consumed": 0}

        # At-least-once delivery: a redelivered consume must charge exactly once. The window
        # is bounded — an unbounded dedupe table grows forever and never forgets a key.
        now = self._now()
        idem_key = self._idempo_key(tenant, idempotency_key) if idempotency_key else ""
        if idem_key:
            prior = await self._store.get(_IDEMPO, idem_key)
            if prior and float(prior.get("expires_at", 0)) > now:
                return {**prior["result"], "idempotent": True}

        ckey = self.counter_key(tenant, key, pkey)

        if sp["unlimited"]:
            _, after = await self._store.incr_if_below(_COUNTERS, ckey, int(amount), None)
            result = {**_ent.evaluate(sp, after - int(amount), int(amount)),
                      "period": pkey, "consumed": int(amount), "current": after}
        else:
            limit = int(sp["limit"])
            # `block` is bounded by the limit. The permissive policies are unbounded here and
            # the excess is billed/flagged instead — the bound lives in the policy, not the store.
            bound = None if sp["overage"] in ("allow_and_bill", "soft") else limit
            applied, after = await self._store.incr_if_below(_COUNTERS, ckey, int(amount), bound)
            before = after - int(amount) if applied else after
            result = _ent.evaluate(sp, before, int(amount))
            result["allowed"] = bool(applied and result["allowed"])
            result["current"] = after if applied else before
            result["remaining"] = max(0, limit - result["current"])
            result["period"] = pkey
            result["consumed"] = int(amount) if applied else 0

            sub_id = resolved.get("subscription_id") or ""
            if (applied and result["overage"] > 0 and sp["overage"] == "allow_and_bill"
                    and self._meter and sub_id):
                # Record the EXCESS UNITS where run_overage() already aggregates them
                # (UsageStore, keyed by subscription + metric). It prices and invoices them
                # at period end. Non-fatal: metering must never fail the customer's request.
                try:
                    await self._meter.record(tenant, sub_id, key, int(result["overage"]))
                except Exception:      # noqa: BLE001
                    pass

        # Record only a SUCCESSFUL consume: a denied attempt must be retryable once the
        # tenant upgrades or the period rolls over.
        if idem_key and result.get("allowed"):
            ttl = int(self._cfg.get("idempotency_ttl", 86_400))
            # atomic: ok — the CONSUME itself is atomic (incr_if_below, above); this _IDEMPO row is a
            # best-effort retry cache so a re-sent (sequential) request returns the cached result. A
            # concurrent same-key double-fire would over-meter by the duplicate only, and a
            # reserve-before-consume rewrite of this money path risks denying legitimate usage — the
            # atomic counter, not this cache, is the hard invariant.
            await self._store.put(_IDEMPO, idem_key,
                                  {"key": idempotency_key, "tenant": tenant, "result": result,
                                   "expires_at": now + ttl})
        return result

    async def release(self, *, tenant: str, key: str, amount: int = 1) -> dict:
        """Refund a counter unit after a failed action. Never refused, clamped at 0."""
        if int(amount) < 0:
            return {"released": 0, "key": key, "error": "NEGATIVE_AMOUNT"}
        sp, pkey, _ = await self._spec_for(tenant, key)
        if not _ent.is_metered(sp):
            return {"released": 0, "key": key, "error": "NOT_A_COUNTER"}
        _, after = await self._store.incr_if_below(
            _COUNTERS, self.counter_key(tenant, key, pkey), -int(amount), None)
        return {"released": int(amount), "current": after, "period": pkey, "key": key}

    async def set_usage(self, *, tenant: str, key: str, value: int) -> dict:
        """Report a GAUGE's absolute value. Idempotent by nature: last write wins."""
        sp, pkey, _ = await self._spec_for(tenant, key)
        if sp["kind"] != "gauge":
            return {"key": key, "error": "NOT_A_GAUGE",
                    "hint": "counters are consumed, not set"}
        n = max(0, int(value))
        await self._store.put(_COUNTERS, self.counter_key(tenant, key, pkey), {"n": n})
        out = _ent.evaluate(sp, n, 0)
        # A downgrade can leave a tenant above the new limit. We report it and block NEW
        # consumption; we never delete anyone's data to make the number fit.
        return {**out, "current": n, "period": pkey}

    async def summary(self, tenant: str, keys: list[str] | None = None) -> dict:
        """What the UI needs: '18 of 25 seats'. Without it, clients count for themselves."""
        resolved = await self._limits(tenant)
        limits = resolved["limits"]
        out: dict[str, Any] = {}
        for key in (keys if keys is not None else list(limits or {})):
            try:
                out[key] = await self.check(tenant=tenant, key=key, amount=0)
            except _ent.EntitlementError as exc:
                out[key] = {"allowed": False, "error": str(exc)}
        return {"tenant": tenant, "entitlements": out}
