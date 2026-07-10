"""Wallet — credit, spend, hold, refund, expire.

Design, in one paragraph. The **ledger is the truth**; the wallet row is a **projection** of it,
because summing the ledger on every read is an O(n) hot path that grows forever. They move
together, and `reconcile()` recomputes the balance from the ledger and must agree. Spending is
planned against `available` (balance minus active holds), never the raw balance — otherwise a
hold is decoration and two requests spend the same money. Nothing is ever partially applied: a
spend that cannot be paid raises and mutates nothing.

Three things this file does that the implementations we studied do not:

* **Reserved totals live IN the wallet row.** Deriving them by scanning the holds table would
  put a full scan on every single spend.
* **Idempotency is a keyed lookup.** The reference answers "seen this source?" with
  `SELECT … WHERE source = ?` over the ledger — cheap with its index, a full ledger scan on our
  generic DataStore.
* **Holds expire, and a sweeper enforces it.** A hold with no expiry locks a customer's credits
  permanently the first time a phone dies between reserve and confirm.

Crash-safety. Every mutation is ONE compare-and-swap on the wallet row, which carries the
ledger row it implies (`pending`). The ledger append that follows is a deterministic,
idempotent write of that staged row. If the process dies in between, the next write (or
`reconcile`) replays it from the wallet row: the ledger can lag by one entry, but it can never
disagree, and no money moves without its record.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any

from app.core import funds as _funds
from app.core.models import (CLAWBACK, CREDIT, HOLD_ACTIVE, HOLD_CONFIRMED, HOLD_EXPIRED,
                             HOLD_RELEASED, REFUND, SPEND, Grant, Hold, LedgerEntry, Wallet,
                             new_id)
from app.core.money import MoneyError, require_non_negative

_WALLETS = "wallets"
_LEDGER = "ledger"
_HOLDS = "holds"
_GRANTS = "grants"
_IDEMPO = "wallet_idempo"
_SCAN = 1_000_000


class WalletError(Exception):
    pass


class Conflict(WalletError):
    """Too much contention on one wallet. Nothing was written; the caller may retry."""


def _ledger_key(tenant: str, seq: int) -> str:
    return f"{tenant}:{int(seq):012d}"


class WalletService:
    def __init__(self, *, store, clock, config: dict[str, Any], event_bus=None) -> None:
        self._store = store
        self._clock = clock
        self._cfg = config
        self._bus = event_bus
        self._funds = _funds.parse(config["funds"])
        self._names = {f.name for f in self._funds}
        self._vat_bp = int(config["vat_bp"])
        self._currency = config["currency"]

    # ── wallet row ──────────────────────────────────────────────────────────────
    async def _wallet(self, tenant: str) -> Wallet:
        row = await self._store.get(_WALLETS, tenant)
        return Wallet.from_dict(row) if row else Wallet(tenant=tenant)

    async def _flush(self, w: Wallet) -> None:
        """Replay the ledger row staged by the last CAS, if it never landed.

        Deterministic key + identical content ⇒ writing it twice is a no-op.
        """
        if not w.pending:
            return
        key = _ledger_key(w.tenant, int(w.pending["seq"]))
        if await self._store.get(_LEDGER, key) is None:
            await self._store.put(_LEDGER, key, w.pending)

    async def _mutate(self, tenant: str, *, funds_delta: dict[str, int] | None = None,
                      held_delta: dict[str, int] | None = None,
                      entry: dict[str, Any] | None = None) -> dict[str, Any]:
        """THE atomic step: funds, holds and the staged ledger row move in one CAS.

        `funds_delta`/`held_delta` are signed. Retries on contention; on give-up nothing was
        written, because a losing CAS changes nothing by construction.

        Losers back off with FULL JITTER before retrying. Without it every loser retries in
        the same instant and collides again: measured against real PostgreSQL, 50 concurrent
        spends on one wallet produced 33 `409`s at 8 retries. The contention is safe — nothing
        is written and `reconcile` stays consistent — but it is a retry the caller should not
        have to make.
        """
        backoff_ms = int(self._cfg.get("cas_backoff_ms", 0))
        for attempt in range(int(self._cfg["cas_max_retries"])):
            if attempt and backoff_ms:
                cap = min(backoff_ms * (2 ** (attempt - 1)), backoff_ms * 32)
                await asyncio.sleep(random.uniform(0, cap) / 1000.0)  # noqa: S311 - jitter, not crypto
            w = await self._wallet(tenant)
            await self._flush(w)                          # never stage over an unwritten row

            new_funds = dict(w.funds)
            for name, delta in (funds_delta or {}).items():
                new_funds[name] = int(new_funds.get(name, 0)) + int(delta)
                if new_funds[name] < 0:
                    raise _funds.InsufficientFunds(needed=abs(int(delta)),
                                                   available=int(w.funds.get(name, 0)))
            new_held = dict(w.held)
            for name, delta in (held_delta or {}).items():
                new_held[name] = int(new_held.get(name, 0)) + int(delta)
                if new_held[name] < 0:
                    new_held[name] = 0                    # releasing more than held: clamp
            new_held = {k: v for k, v in new_held.items() if v}

            seq = w.seq
            staged = None
            if entry is not None:
                seq = w.seq + 1
                staged = {**entry, "seq": seq, "balance_after": _funds.total(new_funds)}

            candidate = Wallet(tenant=tenant, funds=new_funds, held=new_held, version=w.version,
                               seq=seq, pending=staged, updated=self._clock.now())
            if not await self._store.cas(_WALLETS, tenant, w.version, candidate.to_dict()):
                continue                                   # raced; re-read and retry

            if staged is not None:
                await self._store.put(_LEDGER, _ledger_key(tenant, seq), staged)
            return {"funds": new_funds, "held": new_held, "balance": _funds.total(new_funds),
                    "entry": staged}
        raise Conflict("WALLET_CONTENTION")

    def _entry(self, tenant: str, entry_type: str, applied: dict[str, int], sign: int,
               plan: dict[str, Any] | None = None, source: str = "", reason: str = "",
               ref: str = "") -> dict[str, Any]:
        return LedgerEntry(
            id=new_id("led_"), tenant=tenant, seq=0, type=entry_type, applied=dict(applied),
            net=int((plan or {}).get("net", 0)), vat=int((plan or {}).get("vat", 0)),
            discount=int((plan or {}).get("discount", 0)),
            amount=sum(int(v) for v in applied.values()) * sign,
            currency=self._currency, source=source, reason=reason, ref=ref,
            created=self._clock.now()).to_dict()

    # ── reads (no scans) ────────────────────────────────────────────────────────
    async def balance(self, tenant: str) -> dict[str, Any]:
        w = await self._wallet(tenant)
        avail = _funds.available(w.funds, w.held)
        return {"tenant": tenant, "currency": self._currency, "funds": w.funds,
                "held": w.held, "available": avail, "balance": _funds.total(w.funds),
                "available_total": _funds.total(avail)}

    async def ledger(self, tenant: str, limit: int = 100, before_seq: int | None = None) -> list[dict]:
        """Bounded. A statement endpoint that returns the whole ledger is a denial of service."""
        w = await self._wallet(tenant)
        await self._flush(w)
        limit = max(1, min(int(limit), int(self._cfg["ledger_list_limit"])))
        top = int(before_seq) - 1 if before_seq else w.seq
        out = []
        for seq in range(top, max(0, top - limit), -1):    # keyed reads, newest first
            row = await self._store.get(_LEDGER, _ledger_key(tenant, seq))
            if row:
                out.append(row)
        return out

    async def reconcile(self, tenant: str) -> dict[str, Any]:
        """Prove the projection equals the truth: sum(ledger) == the materialized balance."""
        w = await self._wallet(tenant)
        await self._flush(w)
        derived: dict[str, int] = {}
        for seq in range(1, w.seq + 1):
            row = await self._store.get(_LEDGER, _ledger_key(tenant, seq))
            if not row:
                return {"tenant": tenant, "consistent": False, "missing_seq": seq}
            sign = 1 if row["type"] in (CREDIT, REFUND) else -1
            for name, amount in (row.get("applied") or {}).items():
                derived[name] = derived.get(name, 0) + sign * int(amount)
        stored = {k: int(v) for k, v in w.funds.items() if int(v)}
        derived = {k: v for k, v in derived.items() if v}
        return {"tenant": tenant, "consistent": stored == derived, "stored": stored,
                "derived": derived, "entries": w.seq}

    # ── idempotency (keyed; never a ledger scan) ────────────────────────────────
    async def _replay(self, tenant: str, op: str, source: str) -> dict | None:
        if not source:
            return None
        row = await self._store.get(_IDEMPO, f"{tenant}|{op}|{source}")
        if row and float(row.get("expires_at", 0)) > self._clock.now():
            return {**row["result"], "idempotent": True}
        return None

    async def _remember(self, tenant: str, op: str, source: str, result: dict) -> None:
        if not source:
            return
        await self._store.put(_IDEMPO, f"{tenant}|{op}|{source}",
                              {"result": result,
                               "expires_at": self._clock.now() + int(self._cfg["idempotency_ttl"])})

    # ── credit ──────────────────────────────────────────────────────────────────
    async def credit(self, *, tenant: str, fund: str, amount: int, source: str = "",
                     reason: str = "", expires_at: float | None = None) -> dict[str, Any]:
        """Add money. Credit a top-up only once the PSP has SETTLED it — recording an intent is
        not money. `source` makes a redelivered settlement webhook safe."""
        require_non_negative(amount, "amount")
        if fund not in self._names:
            raise WalletError(f"UNKNOWN_FUND:{fund}")
        prior = await self._replay(tenant, CREDIT, source)
        if prior:
            return prior

        entry = self._entry(tenant, CREDIT, {fund: int(amount)}, +1, source=source, reason=reason)
        out = await self._mutate(tenant, funds_delta={fund: int(amount)}, entry=entry)

        if expires_at:
            grant = Grant(id=new_id("grt_"), tenant=tenant, fund=fund, amount=int(amount),
                          expires_at=float(expires_at), created=self._clock.now())
            await self._store.put(_GRANTS, grant.id, grant.to_dict())
            out["grant_id"] = grant.id

        result = {"credited": int(amount), "fund": fund, **out}
        await self._remember(tenant, CREDIT, source, result)
        await self._emit("wallet.credited", {"tenant": tenant, "fund": fund, "amount": int(amount)})
        return result

    # ── spend ───────────────────────────────────────────────────────────────────
    async def _plan(self, w: Wallet, net: int) -> _funds.SpendPlan:
        """Plan against AVAILABLE. Planning against the raw balance spends held money."""
        return _funds.plan_spend(net, _funds.available(w.funds, w.held), self._funds, self._vat_bp)

    async def spend(self, *, tenant: str, net: int, source: str = "",
                    reason: str = "") -> dict[str, Any]:
        """Direct debit, no hold. Refuses rather than overdraw; refusal mutates nothing."""
        prior = await self._replay(tenant, SPEND, source)
        if prior:
            return prior
        plan = await self._plan(await self._wallet(tenant), net)
        entry = self._entry(tenant, SPEND, plan.applied, -1, plan=plan.to_dict(),
                            source=source, reason=reason)
        out = await self._mutate(tenant, funds_delta={k: -v for k, v in plan.applied.items()},
                                 entry=entry)
        result = {"spent": sum(plan.applied.values()), "plan": plan.to_dict(), **out}
        await self._remember(tenant, SPEND, source, result)
        await self._emit("wallet.spent", {"tenant": tenant, "net": plan.net, "vat": plan.vat,
                                          "currency": self._currency, "source": source})
        return result

    # ── holds ───────────────────────────────────────────────────────────────────
    async def reserve(self, *, tenant: str, net: int, source: str = "",
                      reason: str = "") -> dict[str, Any]:
        """Hold funds. Money leaves the balance only on confirm; the hold expires regardless."""
        prior = await self._replay(tenant, "reserve", source)
        if prior:
            return prior
        plan = await self._plan(await self._wallet(tenant), net)

        # The hold row is written BEFORE the funds are reserved, and only becomes `counted`
        # once the CAS lands. A crash in between leaves an uncounted row the sweeper discards,
        # never a reservation nobody can find.
        hold = Hold(id=new_id("hld_"), tenant=tenant, applied=dict(plan.applied),
                    plan=plan.to_dict(), state=HOLD_ACTIVE, counted=False,
                    expires_at=self._clock.now() + int(self._cfg["hold_ttl"]),
                    source=source, reason=reason, created=self._clock.now())
        await self._store.put(_HOLDS, hold.id, hold.to_dict())
        try:
            await self._mutate(tenant, held_delta=dict(plan.applied))
        except Exception:
            await self._store.delete(_HOLDS, hold.id)       # nothing was reserved
            raise
        hold.counted = True
        await self._store.put(_HOLDS, hold.id, hold.to_dict())

        result = {"hold_id": hold.id, "expires_at": hold.expires_at, "plan": plan.to_dict(),
                  "amount": hold.amount}
        await self._remember(tenant, "reserve", source, result)
        return result

    async def _hold(self, hold_id: str) -> Hold:
        row = await self._store.get(_HOLDS, hold_id)
        if not row:
            raise WalletError("HOLD_NOT_FOUND")
        return Hold.from_dict(row)

    async def confirm(self, *, hold_id: str) -> dict[str, Any]:
        """Consume a hold: the money finally leaves. A replay must not charge twice."""
        hold = await self._hold(hold_id)
        if hold.state == HOLD_CONFIRMED:
            return {"hold_id": hold_id, "state": HOLD_CONFIRMED, "idempotent": True}
        if hold.state != HOLD_ACTIVE:
            raise WalletError(f"HOLD_{hold.state.upper()}")
        if hold.expires_at <= self._clock.now():
            raise WalletError("HOLD_EXPIRED")               # expired but unswept: never charge it

        entry = self._entry(hold.tenant, SPEND, hold.applied, -1, plan=hold.plan,
                            source=hold.source, reason=hold.reason, ref=hold.id)
        # Debit the funds and drop the reservation in the SAME atomic step: a crash between the
        # two would either double-count the hold or spend money that was never reserved.
        out = await self._mutate(hold.tenant,
                                 funds_delta={k: -v for k, v in hold.applied.items()},
                                 held_delta={k: -v for k, v in hold.applied.items()},
                                 entry=entry)
        hold.state = HOLD_CONFIRMED
        await self._store.put(_HOLDS, hold.id, hold.to_dict())
        await self._emit("wallet.spent", {"tenant": hold.tenant, "net": hold.plan.get("net", 0),
                                          "vat": hold.plan.get("vat", 0),
                                          "currency": self._currency, "source": hold.source})
        return {"hold_id": hold.id, "state": HOLD_CONFIRMED, **out}

    async def release(self, *, hold_id: str) -> dict[str, Any]:
        """Cancel a hold. Nothing was debited, so nothing is refunded — the money is freed."""
        hold = await self._hold(hold_id)
        if hold.state in (HOLD_RELEASED, HOLD_EXPIRED):
            return {"hold_id": hold_id, "state": hold.state, "idempotent": True}
        if hold.state == HOLD_CONFIRMED:
            raise WalletError("HOLD_ALREADY_CONFIRMED")     # that needs refund(), not release()
        if hold.counted:
            await self._mutate(hold.tenant, held_delta={k: -v for k, v in hold.applied.items()})
        hold.state = HOLD_RELEASED
        await self._store.put(_HOLDS, hold.id, hold.to_dict())
        return {"hold_id": hold.id, "state": HOLD_RELEASED, "freed": hold.amount}

    # ── refund ──────────────────────────────────────────────────────────────────
    async def refund(self, *, tenant: str, seq: int, source: str = "",
                     reason: str = "") -> dict[str, Any]:
        """Reverse a spend INTO THE FUNDS IT CAME FROM.

        Refunding a spend that consumed an expiring gift back into cash silently converts that
        gift into real money. The split is read off the original spend's ledger row — never
        guessed, never defaulted to a 'main' fund.
        """
        prior = await self._replay(tenant, REFUND, source)
        if prior:
            return prior
        row = await self._store.get(_LEDGER, _ledger_key(tenant, seq))
        if not row:
            raise WalletError("LEDGER_ENTRY_NOT_FOUND")
        if row["type"] != SPEND:
            raise WalletError("ONLY_A_SPEND_CAN_BE_REFUNDED")

        applied = {k: int(v) for k, v in row["applied"].items()}
        entry = self._entry(tenant, REFUND, applied, +1, source=source, reason=reason,
                            ref=row["id"])
        out = await self._mutate(tenant, funds_delta=applied, entry=entry)
        result = {"refunded": sum(applied.values()), "applied": applied, **out}
        await self._remember(tenant, REFUND, source, result)
        return result

    # ── sweepers (cron; scans are fine here, they are not the hot path) ─────────
    async def sweep_holds(self) -> dict[str, Any]:
        """Release holds whose owner never came back.

        Without this a hold is a permanent lock: the implementation we studied has no
        age-based release and no `expires_at` on the reservation, so a phone that dies between
        authorize and submit strands those credits forever — while the balance still shows them.
        """
        now = self._clock.now()
        limit = int(self._cfg["hold_sweep_limit"])
        swept = freed = 0
        for row in await self._store.list(_HOLDS, _SCAN):
            if swept >= limit:
                break
            if row.get("state") != HOLD_ACTIVE or float(row.get("expires_at", 0)) > now:
                continue
            hold = Hold.from_dict(row)
            if hold.counted:
                await self._mutate(hold.tenant,
                                   held_delta={k: -v for k, v in hold.applied.items()})
            hold.state = HOLD_EXPIRED
            await self._store.put(_HOLDS, hold.id, hold.to_dict())
            swept += 1
            freed += hold.amount
        return {"swept": swept, "freed": freed, "limit": limit}

    async def sweep_grants(self) -> dict[str, Any]:
        """Claw back the UNUSED remainder of an expired grant. The customer keeps what they spent."""
        now = self._clock.now()
        limit = int(self._cfg["grant_sweep_limit"])
        expired = clawed = 0
        for row in await self._store.list(_GRANTS, _SCAN):
            if expired >= limit:
                break
            if row.get("state") != "active" or not row.get("expires_at") \
                    or float(row["expires_at"]) > now:
                continue
            grant = Grant.from_dict(row)
            w = await self._wallet(grant.tenant)
            # Never claw back money that is currently reserved for an in-flight action.
            spare = int(w.funds.get(grant.fund, 0)) - int(w.held.get(grant.fund, 0))
            remaining = max(0, min(spare, grant.amount))
            if remaining > 0:
                entry = self._entry(grant.tenant, CLAWBACK, {grant.fund: remaining}, -1,
                                    source=f"clawback:{grant.id}", reason="grant expired")
                await self._mutate(grant.tenant, funds_delta={grant.fund: -remaining}, entry=entry)
                clawed += remaining
            grant.state = "expired"
            grant.swept = remaining
            await self._store.put(_GRANTS, grant.id, grant.to_dict())
            expired += 1
        return {"expired": expired, "clawed": clawed, "limit": limit}

    # ── events (optional consumer) ──────────────────────────────────────────────
    async def _emit(self, topic: str, payload: dict) -> None:
        """billing-service invoices these IF it is listening. The wallet works standalone."""
        if self._bus is None:
            return
        try:
            await self._bus.publish(topic, payload)
        except Exception:      # noqa: BLE001 — a bus outage must never lose a customer's money
            pass


__all__ = ["WalletService", "WalletError", "Conflict", "MoneyError"]
