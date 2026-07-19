"""Proving that whoever signed up can actually read the address they claimed.

Without this, `register` accepts any string that looks like an email. Anyone can sign up as
`cfo@bank.com` — and every downstream service that treats the address as an identity (a password
reset, an invite, a notification, an audit actor) inherits that lie. Our own invite flow exists
BECAUSE an unproven address is not an identity; self-service registration quietly reintroduced the
problem the invite flow was built to avoid.

THE TOKEN IS A CREDENTIAL. It grants the ability to activate an account, so it is treated like
one:

    minted from secrets.token_urlsafe(32)   — a CSPRNG, not uuid4 and not a counter
    returned to the caller exactly ONCE     — the raw value is never stored, anywhere
    stored as a KEYED digest                — a stolen table is a table of useless hashes, and
                                              keying it means the thief needs the pepper too
    compared in constant time               — a timing oracle on a prefix is still an oracle
    single-use, enforced by DELETION        — see below; this is the interesting part
    bounded in lifetime and in number       — both from config

SINGLE-USE IS ENFORCED BY DELETING THE ROW, NOT BY SETTING A FLAG.

The obvious implementation is `row["used"] = True` followed by a write. It has a race: two
requests arriving together both read `used == False`, both pass the check, and both proceed. The
window is small and entirely reachable — a double-clicked link, a mail client prefetching URLs,
a retry. Our DataStore port has no compare-and-swap, and adding one would change a contract
shared by twenty services.

So the consume step is `delete()`, which returns whether the row was actually there. Exactly one
caller can get True for a given key. The loser is told the token is invalid, which is the truth:
by then it has been spent. Deletion also means an expired token leaves nothing behind to sweep.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import secrets
from typing import Any

_COLL = "email_verifications"


def _now_ts() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


class VerificationStore:
    def __init__(self, store, *, pepper: str = "", ttl_seconds: int = 86_400,
                 max_outstanding: int = 3) -> None:
        self._store = store
        self._pepper = pepper
        self._ttl = ttl_seconds
        self._max_outstanding = max_outstanding

    def _digest(self, token: str) -> str:
        """Keyed when a pepper is configured, plain SHA-256 when it is not.

        A 256-bit random token needs no slow KDF — there is nothing to brute-force. The pepper
        buys something different: an attacker who steals the DATABASE still cannot recognise a
        token they separately obtained, because the digest depends on a secret that is not in the
        table. Degrading to plain SHA-256 without one keeps a zero-config deployment working
        rather than silently disabling verification.
        """
        if self._pepper:
            return hmac.new(self._pepper.encode(), token.encode(), hashlib.sha256).hexdigest()
        return hashlib.sha256(token.encode()).hexdigest()

    async def issue(self, *, user_id: str, tenant: str, address: str) -> str:
        """Mint a FRESH token. The RAW value is returned once and never stored.

        Two tempting alternatives are both worse. Reusing the live token on every resend (so old
        emails keep working) means the token is NEVER rotated and each resend pushes its expiry
        further out — one address leaked into a mail forward or a proxy log stays valid
        indefinitely. Invalidating all previous tokens on resend means the second email breaks the
        first, and users click the one they find first.

        Minting fresh while keeping a BOUNDED number alive gets both properties: earlier links
        keep working, every token still ages out on its own clock, and the number of live
        credentials for one account has a ceiling.
        """
        await self._prune(user_id)
        token = secrets.token_urlsafe(32)
        key = self._digest(token)
        await self._store.put(_COLL, key, {
            "id": key,
            "user_id": user_id,
            "tenant": tenant,
            # The address is recorded so a token cannot be redeemed after the account changed
            # address — otherwise an old link proves the NEW address, which nobody proved.
            "address": address,
            "exp": _now_ts() + self._ttl,
            "issued_at": _now_ts(),
        })
        return token

    async def redeem(self, token: str) -> dict[str, Any] | None:
        """Consume a token. Returns its row, or None if unknown, expired, or already spent."""
        key = self._digest(token)
        row = await self._store.get(_COLL, key)
        if not row:
            return None
        # The lookup already matched, so this compare is belt-and-braces — but a caller who can
        # learn "a row exists for this key" from timing has learned something they should not.
        if not hmac.compare_digest(str(row.get("id", "")), key):
            return None
        if float(row.get("exp", 0)) <= _now_ts():
            await self._store.delete(_COLL, key)      # expired: take it out of the way
            return None
        # THE ATOMIC STEP. Exactly one concurrent caller sees True here; the other is told the
        # token is invalid, which by then it is.
        if not await self._store.delete(_COLL, key):
            return None
        return row

    async def recently_issued(self, user_id: str, cooldown_seconds: int) -> bool:
        """Has this account been sent a token within the cooldown?

        A per-ADDRESS cooldown, not merely a per-IP rate limit. An IP limit protects the service;
        it does nothing for the person being mail-bombed, because an attacker who knows an address
        simply spreads the requests across addresses of their own choosing — the victim's inbox
        fills either way. The limit that protects the human has to be keyed on the human.
        """
        if cooldown_seconds <= 0:
            return False
        rows, _ = await self._store.find(_COLL, {"user_id": user_id}, limit=self._max_outstanding + 5,
                                         sort=("issued_at", "desc"))
        if not rows:
            return False
        return (_now_ts() - float(rows[0].get("issued_at", 0))) < cooldown_seconds

    async def _prune(self, user_id: str) -> None:
        """Bound how many live tokens one account may have.

        Every resend mints a credential. Without a cap, an attacker who knows an address can
        force an unbounded set of simultaneously-valid tokens into the store — more live keys,
        more chances that one leaks through a mail archive or a proxy log. Oldest go first, so a
        legitimate user who clicks resend still finds their newest link works.
        """
        rows, _ = await self._store.find(_COLL, {"user_id": user_id},
                                         limit=max(1, self._max_outstanding) + 25,
                                         sort=("issued_at", "asc"))
        now = _now_ts()
        live = [r for r in rows if float(r.get("exp", 0)) > now]
        for row in rows:
            if float(row.get("exp", 0)) <= now:
                await self._store.delete(_COLL, str(row.get("id", "")))
        # Keep room for the one we are about to mint.
        for row in live[: max(0, len(live) - max(0, self._max_outstanding - 1))]:
            await self._store.delete(_COLL, str(row.get("id", "")))
