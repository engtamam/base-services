"""Daily quota — the spend ceiling, as distinct from the burst limit.

RATE LIMITING AND QUOTA ANSWER DIFFERENT QUESTIONS. A rate limiter stops a user hammering the
endpoint in a minute; it does nothing about a user who asks a reasonable-looking question every
thirty seconds all day and quietly spends a month of model budget. The second is the one that shows
up on an invoice, so it needs its own ceiling.

A QUOTA IS A RATE LIMIT WITH A LONG WINDOW, so this reuses the limiter port rather than growing an
atomic counter of its own. It inherits three properties a hand-rolled counter would have to
re-earn: the check and the increment are ATOMIC (a read-then-write counter lets N concurrent
requests all observe the same value and all proceed), it is backed by the shared store so it holds
across replicas, and on an outage it degrades to a local budget rather than failing open or closed.

SPEND FIRST, REFUND AN ABSTENTION. There is deliberately no read-only pre-check: the limiter floors
cost at 1, so "just asking" would consume a unit and the later charge would bill the same answer
twice. Instead one atomic spend happens up front, and an answer that turned out to be an abstention
is refunded through the port's refund seam. An abstention did no work worth paying for, and
charging for one would mean a user burns their day discovering a gap in the knowledge base.

TWO CEILINGS, NOT ONE. Per tenant AND per subject. A single tenant-wide counter means the first
enthusiastic user exhausts the day for all their colleagues, and the quota then reads as an outage
to everyone who did nothing wrong. The per-subject ceiling makes one person's spend their own; the
per-tenant ceiling is what actually bounds the bill.
"""
from __future__ import annotations

from dataclasses import dataclass

# One day. Named rather than inlined so the window and the config cannot drift apart.
DAY_SECONDS = 86_400


@dataclass(frozen=True)
class QuotaVerdict:
    allowed: bool
    scope: str = ""          # "subject" | "tenant" — which ceiling refused, for the message
    remaining: int = 0
    retry_after_s: int = 0


class DailyQuota:
    """Both ceilings, over the shared limiter."""

    def __init__(self, limiter, *, per_subject: int, per_tenant: int) -> None:
        self._limiter = limiter
        self._per_subject = per_subject
        self._per_tenant = per_tenant

    @staticmethod
    def _key(scope: str, value: str) -> str:
        return f"assistant:quota:{scope}:{value}"

    def _scopes(self, tenant: str, subject_ref: str):
        """The configured ceilings, subject first.

        Subject before tenant so an individual who is over their own budget is told that, rather
        than being told the tenant is exhausted — which would send them to an administrator who
        cannot help.
        """
        for scope, limit, value in (("subject", self._per_subject, subject_ref),
                                    ("tenant", self._per_tenant, tenant)):
            # 0 means no ceiling, and it is skipped entirely so an unconfigured quota costs no
            # round trip rather than one that always says yes.
            if limit > 0 and value:
                yield scope, limit, value

    async def spend(self, *, tenant: str, subject_ref: str) -> QuotaVerdict:
        """Consume one unit against every configured ceiling, atomically.

        If a later ceiling refuses, the earlier ones are refunded before returning — otherwise a
        request rejected by the tenant ceiling would still have cost the subject a unit, and a user
        would watch their personal budget drain on requests that were never served.
        """
        spent: list[tuple[str, int, str]] = []
        for scope, limit, value in self._scopes(tenant, subject_ref):
            v = await self._limiter.check(self._key(scope, value), cost=1,
                                          limit=limit, window_s=DAY_SECONDS)
            if not v.allowed:
                await self._refund(spent)
                return QuotaVerdict(allowed=False, scope=scope, remaining=0,
                                    retry_after_s=getattr(v, "retry_after_s", 0))
            spent.append((scope, limit, value))
        return QuotaVerdict(allowed=True)

    async def refund(self, *, tenant: str, subject_ref: str) -> None:
        """Give back a unit on every ceiling — used when the answer was an abstention."""
        await self._refund(list(self._scopes(tenant, subject_ref)))

    async def _refund(self, scopes) -> None:
        for scope, limit, value in scopes:
            try:
                await self._limiter.refund(self._key(scope, value), cost=1,
                                           limit=limit, window_s=DAY_SECONDS)
            except Exception:   # noqa: BLE001 — a failed refund must never fail the request
                # The user keeps their answer and loses one unit of budget. The reverse — failing a
                # served request because the bookkeeping failed — costs them the answer as well.
                pass
