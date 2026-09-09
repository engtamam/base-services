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

from app.core import events as _events
from app.core import funds as _funds
from app.core.events import Emitter
from app.core.models import (CLAWBACK, CREDIT, CREDIT_TYPES, EXPIRE, RECLAIM, HOLD_ACTIVE, HOLD_CONFIRMED,
                             HOLD_EXPIRED, HOLD_RELEASED, REFUND, SPEND, WITHDRAW, WITHDRAW_FAILED,
                             WITHDRAW_PAID, WITHDRAW_PENDING, WITHDRAW_REVERSAL, Grant, Hold,
                             LedgerEntry, Wallet, Withdrawal, new_id)
from app.core.money import MoneyError, require_non_negative

_WALLETS = "wallets"
_LEDGER = "ledger"
_HOLDS = "holds"
_GRANTS = "grants"
_WITHDRAWALS = "withdrawals"
_IDEMPO = "wallet_idempo"
_ALERTS = "wallet_alerts"    # the low-balance armed/triggered state, ONE keyed row per tenant
_SWEEP_PAGE = 500        # the sweepers page; they never read a collection whole

# The two low-balance modes. `crossing` warns ONCE, on the mutation that takes the balance under
# the threshold, and re-arms only after it recovers — an alert that fires every spend is noise, and
# noise gets muted. `every_spend` is the running nag some platforms genuinely want.
LOW_CROSSING = "crossing"
LOW_EVERY_SPEND = "every_spend"


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
        self._events = Emitter(
            event_bus,
            fail_closed=str(config.get("event_publish_failure", _events.FAIL_OPEN))
            == _events.FAIL_CLOSED,
            clock=clock)
        # 0 disables the warning entirely: a general platform opts IN to telling its customers
        # anything, and a threshold guessed on their behalf is a wrong threshold for someone.
        self._low_threshold = int(config.get("low_balance_threshold", 0) or 0)
        self._low_mode = str(config.get("low_balance_mode", LOW_CROSSING) or LOW_CROSSING)
        # Empty = the whole balance. A platform with a promo fund and a cash fund usually cares
        # about the fund that pays the bills, not the gift that will expire anyway.
        self._low_fund = str(config.get("low_balance_fund", "") or "")
        # Per-fund expiry policy (validated in config). {} = no fund expires. See _policy().
        self._policies = dict(config.get("fund_expiry_policies", {}) or {})

    def _policy(self, fund: str) -> dict[str, Any] | None:
        """This fund's expiry policy, the "*" default, or None (never expires)."""
        return self._policies.get(fund) or self._policies.get("*")

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
                "available_total": _funds.total(avail),
                # READABLE, not only emitted. The bus is at-most-once with no replay, so a consumer
                # that missed `wallet.balance_low` would otherwise never learn it — for ever.
                **self.low_balance_state(w.funds)}

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
            sign = 1 if row["type"] in CREDIT_TYPES else -1
            for name, amount in (row.get("applied") or {}).items():
                derived[name] = derived.get(name, 0) + sign * int(amount)
        stored = {k: int(v) for k, v in w.funds.items() if int(v)}
        derived = {k: v for k, v in derived.items() if v}
        return {"tenant": tenant, "consistent": stored == derived, "stored": stored,
                "derived": derived, "entries": w.seq}

    # ── idempotency (keyed, ATOMIC; never a ledger scan) ────────────────────────
    # A get-then-put idempotency check has a TOCTOU race: two requests carrying the SAME source (a
    # redelivered webhook, two replicas draining one queue message) both read "not seen", both apply
    # the money, and the source is charged twice. The claim is therefore ATOMIC — a put-if-absent via
    # cas(key, expected_version=0, ...) — so exactly one caller wins the right to mutate. A concurrent
    # duplicate either replays the finished result or, while the winner is mid-flight, is told to
    # retry (Conflict) and gets the replay on its next attempt. Same guarantee as billing's
    # SettlementStore.claim_identity, applied to every money op here.
    async def _claim(self, tenant: str, op: str, source: str) -> tuple[bool, dict | None]:
        """Returns (proceed, prior_result). proceed=True → this caller owns the mutation.
        prior_result set → a finished duplicate to replay. (False, None) → a concurrent attempt holds
        the claim; the caller must not mutate (raise Conflict, the client retries)."""
        if not source:
            return True, None                                # opt-out of idempotency
        key = f"{tenant}|{op}|{source}"
        now = self._clock.now()
        row = await self._store.get(_IDEMPO, key)
        if row and "result" in row and float(row.get("expires_at", 0)) > now:
            return False, {**row["result"], "idempotent": True}     # finished → replay
        if row and row.get("status") == "pending" and float(row.get("expires_at", 0)) > now:
            # Another caller is mid-mutation (unexpired claim). The loser must NOT re-claim — that
            # would let a retry proceed while the winner is still applying the money (double spend).
            # It retries; by then the winner has finalized (replay) or the lease lapsed (re-claim).
            return False, None
        # Absent, or a STALE claim/result (winner presumably died): claim atomically. The pending
        # claim gets a SHORT lease so a crashed winner does not block retries for the full result TTL.
        expected = int(row.get("version", 0)) if row else 0
        lease = int(self._cfg.get("idempotency_claim_lease_seconds", 60) or 60)
        won = await self._store.cas(_IDEMPO, key, expected,
                                    {"status": "pending", "expires_at": now + lease})
        return (True, None) if won else (False, None)

    async def _finalize(self, tenant: str, op: str, source: str, result: dict) -> None:
        if not source:
            return
        # Only the caller that WON the claim finalizes, so there is no contention on this key — a
        # plain put is safe. A late loser never clobbers it: _claim returns the finished result
        # (replay) before it would ever attempt to re-claim, because the row now carries "result".
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
        proceed, prior = await self._claim(tenant, CREDIT, source)
        if prior is not None:
            return prior
        if not proceed:
            raise Conflict("WALLET_OP_IN_PROGRESS")

        entry = self._entry(tenant, CREDIT, {fund: int(amount)}, +1, source=source, reason=reason)
        out = await self._mutate(tenant, funds_delta={fund: int(amount)}, entry=entry)

        # Freeze the fund's ENTIRE expiry policy onto the grant at credit time — expiry AND behaviour —
        # so a later config change (or a deleted policy) can never retroactively forfeit a grant that
        # was issued as preserved/reclaimable. ttl_days=0 (or no policy) ⇒ never expires.
        pol = self._policy(fund) or {}
        if expires_at is None:
            ttl_days = int(pol.get("ttl_days", 0))
            if ttl_days > 0:
                expires_at = self._clock.now() + ttl_days * 86400
        if expires_at:
            grant = Grant(id=new_id("grt_"), tenant=tenant, fund=fund, amount=int(amount),
                          expires_at=float(expires_at),
                          idle_behavior=str(pol.get("idle_behavior", "")),
                          ttl_days=int(pol.get("ttl_days", 0)),
                          notice_days=int(pol.get("notice_days", 0)),
                          created=self._clock.now())
            await self._store.put(_GRANTS, grant.id, grant.to_dict())
            out["grant_id"] = grant.id

        result = {"credited": int(amount), "fund": fund, **out}
        await self._finalize(tenant, CREDIT, source, result)
        await self._emit(_events.WALLET_CREDITED,
                         {"tenant": tenant, "fund": fund, "amount": int(amount)})
        await self._check_low_balance(tenant, out["funds"], debit=False)
        return result

    # ── spend ───────────────────────────────────────────────────────────────────
    def _resolve_scope(self, funds: tuple[str, ...] | None) -> frozenset[str] | None:
        """Validate a caller's fund allowlist. None = every fund (default). A named subset restricts
        the spend to exactly those funds. Fail LOUD on the two footguns: an EMPTY tuple (a scope of
        zero funds can only ever refuse — almost certainly a caller bug, not an intent to spend
        nothing) and an UNKNOWN fund name (a typo must not silently become 'insufficient' against a
        fund that does not exist). Money path: an ambiguous restriction is refused, never guessed."""
        if funds is None:
            return None
        names = tuple(funds)
        if not names:
            raise WalletError("FUNDS_SCOPE_EMPTY")
        unknown = [n for n in names if n not in self._names]
        if unknown:
            raise WalletError(f"UNKNOWN_FUND:{unknown[0]}")
        return frozenset(names)

    async def _plan(self, w: Wallet, net: int,
                    allowed: frozenset[str] | None = None) -> _funds.SpendPlan:
        """Plan against AVAILABLE. Planning against the raw balance spends held money."""
        return _funds.plan_spend(net, _funds.available(w.funds, w.held), self._funds, self._vat_bp,
                                 allowed=allowed)

    async def spend(self, *, tenant: str, net: int, source: str = "",
                    reason: str = "", funds: tuple[str, ...] | None = None) -> dict[str, Any]:
        """Direct debit, no hold. Refuses rather than overdraw; refusal mutates nothing.

        `funds` restricts the debit to a named subset — a spend for one purpose (e.g. an AI-usage
        credit) may be scoped to its OWN fund so it can never drain another purpose's balance. Omit
        for the historical all-funds behaviour."""
        allowed = self._resolve_scope(funds)
        proceed, prior = await self._claim(tenant, SPEND, source)
        if prior is not None:
            return prior
        if not proceed:
            raise Conflict("WALLET_OP_IN_PROGRESS")
        plan = await self._plan(await self._wallet(tenant), net, allowed)
        entry = self._entry(tenant, SPEND, plan.applied, -1, plan=plan.to_dict(),
                            source=source, reason=reason)
        out = await self._mutate(tenant, funds_delta={k: -v for k, v in plan.applied.items()},
                                 entry=entry)
        result = {"spent": sum(plan.applied.values()), "plan": plan.to_dict(), **out}
        await self._finalize(tenant, SPEND, source, result)
        await self._emit(_events.WALLET_SPENT,
                         {"tenant": tenant, "net": plan.net, "vat": plan.vat,
                          "currency": self._currency, "source": source})
        await self._check_low_balance(tenant, out["funds"], debit=True)
        return result

    # ── withdraw / payout ─────────────────────────────────────────────────────────
    def _withdrawable(self) -> tuple[str, ...]:
        return _funds.withdrawable_names(self._funds)

    async def withdraw(self, *, tenant: str, amount: int, destination: str = "",
                       source: str = "", reason: str = "") -> dict[str, Any]:
        """Move money OUT of the platform (a payout). Debits ONLY withdrawable funds — never a
        customer's promo/locked credit — and records a PENDING Withdrawal a payout sidecar executes.

        The ABILITY to withdraw is a capability gated at the route (issued after KYC): who may take
        money out is an account decision. THIS layer enforces WHICH funds may leave and that the
        AVAILABLE balance (minus holds) covers it, and it never overdraws. Idempotent by `source`, so
        a retried request never pays out twice.
        """
        amount = int(amount)
        if amount <= 0:
            raise WalletError("WITHDRAW_AMOUNT_MUST_BE_POSITIVE")
        withdrawable = self._withdrawable()
        if not withdrawable:
            raise WalletError("NO_WITHDRAWABLE_FUND")   # nothing on this platform may be paid out
        minimum = int(self._cfg.get("withdraw_min", 0) or 0)
        if amount < minimum:
            raise WalletError(f"WITHDRAW_BELOW_MINIMUM:{minimum}")
        proceed, prior = await self._claim(tenant, WITHDRAW, source)
        if prior is not None:
            return prior
        if not proceed:
            raise Conflict("WALLET_OP_IN_PROGRESS")
        w = await self._wallet(tenant)
        avail = _funds.available(w.funds, w.held)
        pool = sum(int(avail.get(n, 0)) for n in withdrawable)
        if pool < amount:
            raise _funds.InsufficientFunds(needed=amount, available=pool)
        # Draw from withdrawable funds in spend-priority order; never touch a locked fund.
        applied: dict[str, int] = {}
        remaining = amount
        for f in self._funds:
            if not f.withdrawable or remaining <= 0:
                continue
            take = min(int(avail.get(f.name, 0)), remaining)
            if take > 0:
                applied[f.name] = take
                remaining -= take
        wd = Withdrawal(id=new_id("wd_"), tenant=tenant, amount=amount, applied=dict(applied),
                        destination=destination, state=WITHDRAW_PENDING, source=source,
                        reason=reason, created=self._clock.now())
        await self._store.put(_WITHDRAWALS, wd.id, wd.to_dict())
        entry = self._entry(tenant, WITHDRAW, applied, -1, source=source, reason=reason, ref=wd.id)
        try:
            out = await self._mutate(tenant, funds_delta={k: -v for k, v in applied.items()},
                                     entry=entry)
        except Exception:
            await self._store.delete(_WITHDRAWALS, wd.id)     # the debit never landed
            raise
        result = {"withdrawal_id": wd.id, "amount": amount, "applied": dict(applied),
                  "state": WITHDRAW_PENDING, **out}
        await self._finalize(tenant, WITHDRAW, source, result)
        return result

    async def settle_withdrawal(self, *, withdrawal_id: str, paid: bool,
                                source: str = "") -> dict[str, Any]:
        """Record the payout sidecar's result. `paid=True` marks it paid; `paid=False` REVERSES the
        debit — a transfer that did not happen must never cost the customer money. Idempotent on the
        withdrawal's state: settling an already-settled withdrawal moves nothing (the transition out
        of `pending` is the guard, so a redelivered callback cannot double-pay or double-reverse)."""
        row = await self._store.get(_WITHDRAWALS, withdrawal_id)
        if not row:
            raise WalletError("WITHDRAWAL_NOT_FOUND")
        wd = Withdrawal.from_dict(row)
        if wd.state != WITHDRAW_PENDING:
            return {"withdrawal_id": wd.id, "state": wd.state, "idempotent": True}
        if paid:
            wd.state = WITHDRAW_PAID
        else:
            entry = self._entry(wd.tenant, WITHDRAW_REVERSAL, wd.applied, +1,
                                source=source or f"reverse:{wd.id}", reason="payout failed",
                                ref=wd.id)
            await self._mutate(wd.tenant, funds_delta={k: int(v) for k, v in wd.applied.items()},
                               entry=entry)
            wd.state = WITHDRAW_FAILED
        wd.settled_at = self._clock.now()
        await self._store.put(_WITHDRAWALS, wd.id, wd.to_dict())
        return {"withdrawal_id": wd.id, "state": wd.state}

    # ── holds ───────────────────────────────────────────────────────────────────
    async def reserve(self, *, tenant: str, net: int, source: str = "",
                      reason: str = "", funds: tuple[str, ...] | None = None) -> dict[str, Any]:
        """Hold funds. Money leaves the balance only on confirm; the hold expires regardless.

        `funds` scopes the hold to a named subset (same rule as spend). The hold records the exact
        `applied` funds, so release/confirm reverse those same funds — the scope is fixed at reserve
        time and cannot drift."""
        allowed = self._resolve_scope(funds)
        proceed, prior = await self._claim(tenant, "reserve", source)
        if prior is not None:
            return prior
        if not proceed:
            raise Conflict("WALLET_OP_IN_PROGRESS")
        plan = await self._plan(await self._wallet(tenant), net, allowed)

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
        await self._finalize(tenant, "reserve", source, result)
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
        await self._emit(_events.WALLET_SPENT,
                         {"tenant": hold.tenant, "net": hold.plan.get("net", 0),
                          "vat": hold.plan.get("vat", 0),
                          "currency": self._currency, "source": hold.source})
        await self._check_low_balance(hold.tenant, out["funds"], debit=True)
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
        proceed, prior = await self._claim(tenant, REFUND, source)
        if prior is not None:
            return prior
        if not proceed:
            raise Conflict("WALLET_OP_IN_PROGRESS")
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
        await self._finalize(tenant, REFUND, source, result)
        # A refund can lift a wallet back over the line, and a customer never told they recovered
        # is one whose alerting is stuck low for ever.
        await self._check_low_balance(tenant, out["funds"], debit=False)
        return result

    # ── sweepers (cron, not the hot path - but still not a full-table read) ─────
    async def _active(self, collection: str, state: str, limit: int) -> list[dict[str, Any]]:
        """The rows a sweeper can act on: those still in `state`.

        A sweeper is allowed to be slow. It is not allowed to be O(every hold ever placed) when
        the ones it can act on are the small, shrinking set of ACTIVE ones - the rest were
        settled long ago and will never match again. `state` is pushed into the datastore; the
        expiry comparison is a time range and stays in the caller.
        """
        out: list[dict[str, Any]] = []
        cursor = None
        while len(out) < limit:
            rows, cursor = await self._store.find(collection, {"state": state},
                                                  limit=_SWEEP_PAGE, after=cursor,
                                                  sort=("expires_at", "asc"))
            out.extend(rows)
            if not cursor:
                break
        return out

    async def sweep_holds(self) -> dict[str, Any]:
        """Release holds whose owner never came back.

        Without this a hold is a permanent lock: the implementation we studied has no
        age-based release and no `expires_at` on the reservation, so a phone that dies between
        authorize and submit strands those credits forever — while the balance still shows them.
        """
        now = self._clock.now()
        limit = int(self._cfg["hold_sweep_limit"])
        swept = freed = 0
        for row in await self._active(_HOLDS, HOLD_ACTIVE, limit):
            if swept >= limit:
                break
            if float(row.get("expires_at", 0)) > now:
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
        """Act on expired grants per each fund's configured idle_behavior. The customer always keeps
        what they SPENT; only the unspent remainder is touched, and never money currently reserved.

          forfeit — clawback (money gone, irreversible).           [non-refundable service credit]
          dormant — `expire` ledger + state="dormant" (PRESERVED, reclaimable via reclaim()). [SAMA-safe]
          extend  — push expiry forward by ttl_days, grant stays active (no money moves).

        A fund with NO policy defaults to `forfeit` (the historical behaviour, unchanged)."""
        now = self._clock.now()
        limit = int(self._cfg["grant_sweep_limit"])
        expired = clawed = dormant_total = extended = 0
        for row in await self._active(_GRANTS, "active", limit):
            if expired + extended >= limit:
                break
            if not row.get("expires_at") or float(row["expires_at"]) > now:
                continue
            grant = Grant.from_dict(row)
            # Prefer the policy FROZEN on the grant at credit time; fall back to the live policy only
            # for a legacy grant issued before freezing existed. Default forfeit (historical).
            if grant.idle_behavior:
                behavior, ttl_days = grant.idle_behavior, int(grant.ttl_days)
            else:
                pol = self._policy(grant.fund) or {}
                behavior, ttl_days = str(pol.get("idle_behavior", "forfeit")), int(pol.get("ttl_days", 0))

            if behavior == "extend":
                if ttl_days > 0:                      # a 0-ttl extend would busy-loop the sweep
                    grant.expires_at = now + ttl_days * 86400
                    grant.noticed = 0                 # re-arm the pre-expiry notice for the new window
                    await self._store.put(_GRANTS, grant.id, grant.to_dict())
                    extended += 1
                continue

            w = await self._wallet(grant.tenant)
            # Never touch money currently reserved for an in-flight action.
            spare = int(w.funds.get(grant.fund, 0)) - int(w.held.get(grant.fund, 0))
            remaining = max(0, min(spare, grant.amount))
            if remaining > 0:
                is_dormant = behavior == "dormant"
                led_type = EXPIRE if is_dormant else CLAWBACK
                entry = self._entry(grant.tenant, led_type, {grant.fund: remaining}, -1,
                                    source=f"{led_type}:{grant.id}",
                                    reason="grant expired (dormant)" if is_dormant else "grant expired")
                out = await self._mutate(grant.tenant, funds_delta={grant.fund: -remaining},
                                         entry=entry)
                if is_dormant:
                    dormant_total += remaining
                else:
                    clawed += remaining
                await self._check_low_balance(grant.tenant, out["funds"], debit=True)
            grant.state = "dormant" if behavior == "dormant" else "expired"
            grant.swept = remaining
            await self._store.put(_GRANTS, grant.id, grant.to_dict())
            await self._emit(_events.GRANT_EXPIRED, {
                "tenant": grant.tenant, "fund": grant.fund, "grant_id": grant.id,
                "amount": remaining, "behavior": behavior, "reclaimable": behavior == "dormant"})
            expired += 1
        return {"expired": expired, "clawed": clawed, "dormant": dormant_total,
                "extended": extended, "limit": limit}

    async def notice_grants(self) -> dict[str, Any]:
        """Emit a ONE-TIME pre-expiry notice (wallet.grant_expiring) for grants entering their fund's
        notice_days window — the SAMA notice principle and plain good UX: warn before credit lapses.
        Scheduler-driven; `noticed` makes it fire once. Grants on funds with notice_days=0 are skipped."""
        now = self._clock.now()
        limit = int(self._cfg["grant_sweep_limit"])
        noticed = 0
        for row in await self._active(_GRANTS, "active", limit):
            if noticed >= limit:
                break
            if int(row.get("noticed", 0)) or not row.get("expires_at"):
                continue
            grant = Grant.from_dict(row)
            # Frozen notice window (falls back to live policy only for a legacy grant).
            notice_days = int(grant.notice_days) if grant.idle_behavior \
                else int((self._policy(grant.fund) or {}).get("notice_days", 0))
            if notice_days <= 0 or float(grant.expires_at) > now + notice_days * 86400:
                continue
            grant.noticed = 1
            await self._store.put(_GRANTS, grant.id, grant.to_dict())
            await self._emit(_events.GRANT_EXPIRING, {
                "tenant": grant.tenant, "fund": grant.fund, "grant_id": grant.id,
                "amount": int(grant.amount), "expires_at": float(grant.expires_at)})
            noticed += 1
        return {"noticed": noticed, "limit": limit}

    async def reclaim(self, *, grant_id: str, source: str = "", reason: str = "") -> dict[str, Any]:
        """Restore a DORMANT grant's preserved remainder to the spendable balance (ledger `reclaim`).
        The primitive a future 'reclaim idle balance' flow calls — so that flow needs no new model or
        migration. Idempotent on `source`; refuses a grant that is not dormant."""
        row = await self._store.get(_GRANTS, grant_id)
        if not row:
            raise WalletError("GRANT_NOT_FOUND")
        grant = Grant.from_dict(row)
        # Guard BEFORE claiming: an active/expired grant is refused without consuming an idempotency
        # slot; only a dormant (or already-reclaimed, for idempotency) grant proceeds.
        if grant.state not in ("dormant", "reclaimed"):
            raise WalletError("GRANT_NOT_DORMANT")
        amount = int(grant.swept)
        src = source or f"reclaim:{grant.id}"
        proceed, prior = await self._claim(grant.tenant, RECLAIM, src)
        if prior is not None:
            return prior                                   # same source replayed → the stored result
        if not proceed:
            raise Conflict("WALLET_OP_IN_PROGRESS")
        if grant.state == "reclaimed":
            # Already reclaimed under a DIFFERENT source — never credit a dormant grant twice.
            result = {"reclaimed": 0, "fund": grant.fund, "grant_id": grant.id, "already": True,
                      "funds": (await self._wallet(grant.tenant)).funds}
            await self._finalize(grant.tenant, RECLAIM, src, result)
            return result
        out = {"funds": (await self._wallet(grant.tenant)).funds}
        if amount > 0:
            entry = self._entry(grant.tenant, RECLAIM, {grant.fund: amount}, +1,
                                source=src, reason=reason or "dormant grant reclaimed",
                                ref=f"{EXPIRE}:{grant.id}")
            out = await self._mutate(grant.tenant, funds_delta={grant.fund: amount}, entry=entry)
        grant.state = "reclaimed"
        await self._store.put(_GRANTS, grant.id, grant.to_dict())
        result = {"reclaimed": amount, "fund": grant.fund, "grant_id": grant.id, **out}
        await self._finalize(grant.tenant, RECLAIM, src, result)
        await self._emit(_events.GRANT_RECLAIMED,
                         {"tenant": grant.tenant, "fund": grant.fund, "grant_id": grant.id,
                          "amount": amount})
        return result

    # ── events (optional consumer) ──────────────────────────────────────────────
    async def _emit(self, topic: str, payload: dict) -> None:
        """billing-service invoices these IF it is listening. The wallet works standalone.

        Published as an `Event`, never a raw dict: the port and every adapter type it that way, and
        a dict raises inside the subscriber's handler where the bus swallows it — the publisher
        looks successful and the consumer never acts.
        """
        await self._events.emit(topic, payload)

    # ── low balance: warn BEFORE the money runs out ─────────────────────────────
    def _measured(self, funds: dict[str, int]) -> int:
        """What the threshold is compared against: one fund, or the whole balance."""
        return int(funds.get(self._low_fund, 0)) if self._low_fund else _funds.total(funds)

    def low_balance_state(self, funds: dict[str, int]) -> dict[str, Any]:
        """The low-balance facts, derived from the CURRENT balance, for the balance read.

        Derived and not read from the stored alert row on purpose. Events may be lost — the bus is
        at-most-once with no replay — so a consumer must be able to LEARN the fact rather than only
        be told it once. A read recomputed from the balance is self-healing; it is right even if
        every event this wallet ever published was dropped, and right immediately after an operator
        changes the threshold.
        """
        if self._low_threshold <= 0:
            return {"low_balance": False, "low_balance_threshold": 0, "low_balance_fund": ""}
        return {"low_balance": self._measured(funds) < self._low_threshold,
                "low_balance_threshold": self._low_threshold,
                "low_balance_fund": self._low_fund}

    async def _check_low_balance(self, tenant: str, funds: dict[str, int], *,
                                 debit: bool) -> None:
        """Emit the warning on the CROSSING, and re-arm on recovery.

        The armed/triggered flag is PERSISTED in a tenant-keyed row, never held in memory: in
        process memory every replica warns separately about the same crossing, and a restart re-arms
        everything so the next spend warns all over again — which is exactly the noise the crossing
        rule exists to prevent.

        Never raises. A warning is a courtesy; failing a spend that already succeeded because the
        alert bookkeeping had a bad day would be strictly worse than not warning.
        """
        if self._low_threshold <= 0:      # opted out: no state written, no event, nothing to mute
            return
        try:
            low = self._measured(funds) < self._low_threshold
            row = await self._store.get(_ALERTS, tenant) or {}
            # A threshold changed under a live wallet describes a DIFFERENT question, so the old
            # answer is not evidence about the new one. Re-arm: a customer already below the
            # operator's new line is warned once, rather than silently never — the operator just
            # declared this balance low and expects to hear about it.
            was_low = bool(row.get("low")) and int(row.get("threshold", 0)) == self._low_threshold

            if low == was_low and not (low and self._low_mode == LOW_EVERY_SPEND and debit):
                return
            if low != was_low:
                # Win the write before emitting. Two concurrent spends both crossing would
                # otherwise both warn; the loser of the CAS sees the winner already recorded it.
                nxt = {"tenant": tenant, "low": low, "threshold": self._low_threshold,
                       "version": int(row.get("version", 0))}
                if not await self._store.cas(_ALERTS, tenant, int(row.get("version", 0)), nxt):
                    return
            if low:
                await self._emit(_events.BALANCE_LOW, self._low_payload(tenant, funds))
            else:
                await self._emit(_events.BALANCE_RECOVERED, self._low_payload(tenant, funds))
        except Exception:      # noqa: BLE001 — see the docstring: never break the money movement
            pass

    def _low_payload(self, tenant: str, funds: dict[str, int]) -> dict[str, Any]:
        """No person, no reason text: a payload lands in every subscriber's store, and the tenant
        is enough to look up anything more from a service that is allowed to hold it."""
        return {"tenant": tenant, "balance": self._measured(funds),
                "threshold": self._low_threshold, "fund": self._low_fund,
                "currency": self._currency}


__all__ = ["WalletService", "WalletError", "Conflict", "MoneyError"]
