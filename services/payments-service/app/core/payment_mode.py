"""Per-tenant SINGLE-SELECT payment mode — which linking method a tenant's payments use.

The platform exposes a menu of payment MODELS (conduit, marketplace) and never imposes one; each tenant
selects EXACTLY ONE active mode, and the platform may force a single mode fleet-wide (or freeze a
tenant's choice). This is the store + semantics behind that.

WHY ITS OWN PRIMITIVE, NOT OptionGate. OptionGate is independent booleans — several options can be ON at
once. A payment mode is the opposite: it is a RADIO, exactly one value, because a single payment must
resolve to one model (you cannot split one checkout across two models). So this stores one chosen value,
not a set of toggles. It deliberately mirrors OptionGate's precedence and platform-lock idioms so the
operator surface and the enforcement path read it the same familiar way.

RESOLUTION PRECEDENCE (same shape as OptionGate.status):
  1. a PLATFORM lock forces one mode fleet-wide — it wins over any tenant choice (the "force a single
     mode" requirement);
  2. an explicit tenant selection decides that tenant;
  3. a platform default selection applies to tenants with no choice of their own;
  4. the built-in default — MODEL_CONDUIT — so an untouched deployment behaves exactly as today.

Enforcement reads `selected()`; `set()` REPLACES (never accumulates), which is what makes it exactly-one.
An UNKNOWN mode is refused at the write (a typo must never become a silently-wrong routing decision), and
whether the selected mode's PROVIDER actually supports the requested operation is a separate, later check
(the capability assertion in PaymentService) — this primitive only records the choice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.ports.payment_gateway import MODEL_CONDUIT, MODELS

# The universe of modes a tenant may be set to. The platform curates which of these it OFFERS (by
# forcing/defaulting via platform rows); this set is just "what is a valid value at all".
KNOWN_MODES = frozenset(MODELS)
DEFAULT_MODE = MODEL_CONDUIT

_COLL = "payment_mode_selection"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class PaymentModeError(ValueError):
    """A mode selection the caller may not perform, with a reason they can act on."""


class UnknownMode(PaymentModeError):
    """A mode outside KNOWN_MODES — refused so a typo never routes money the wrong way."""


class ModeLocked(PaymentModeError):
    """A change to a locked scope, without force."""


@dataclass(frozen=True)
class ModeState:
    mode: str
    locked: bool
    source: str           # "default" (no row) | "tenant" | "platform"
    set_by: str = ""
    set_at: str = ""


class PaymentModeGate:
    def __init__(self, store, *, now=_utc_now) -> None:
        self._s = store
        self._now = now

    @staticmethod
    def _key(tenant: str) -> str:
        # Length-prefix the tenant (the tenant-scoped-idempotency convention used by payment_requests),
        # so the platform scope ("" -> "0:|payment_mode") can NEVER be hijacked by a tenant literally
        # named "*" or one crafted to collide — a cross-tenant escalation an unprefixed "{tenant}|.." key
        # would allow. Platform is the empty tenant; every real tenant is non-empty and distinct.
        return f"{len(tenant)}:{tenant}|payment_mode"

    def _require_known(self, mode: str) -> str:
        if mode not in KNOWN_MODES:
            raise UnknownMode(f"unknown payment mode {mode!r}; known: {sorted(KNOWN_MODES)}")
        return mode

    @staticmethod
    def _state(row: dict, source: str) -> ModeState:
        return ModeState(mode=str(row.get("mode", "")), locked=bool(row.get("locked")),
                         source=source, set_by=row.get("set_by", ""), set_at=row.get("set_at", ""))

    # ── read (the enforcement path) ─────────────────────────────────────────────
    async def selected(self, tenant: str) -> str:
        """The tenant's ONE active mode (the value create_link routes on)."""
        return (await self.status(tenant=tenant)).mode

    async def status(self, *, tenant: str) -> ModeState:
        # 1) a PLATFORM lock forces one mode fleet-wide — wins over any tenant choice.
        plat = await self._s.get(_COLL, self._key(""))
        if plat and plat.get("locked") and plat.get("mode") in KNOWN_MODES:
            return self._state(plat, "platform")
        # 2) an explicit, still-valid tenant selection.
        if tenant:
            row = await self._s.get(_COLL, self._key(tenant))
            if row and row.get("mode") in KNOWN_MODES:
                return self._state(row, "tenant")
        # 3) a platform default selection (unlocked) for tenants with no choice of their own.
        if plat and plat.get("mode") in KNOWN_MODES:
            return self._state(plat, "platform")
        # 4) the built-in default — behaves exactly as today.
        return ModeState(mode=DEFAULT_MODE, locked=False, source="default")

    # ── write (operator-only, lock-guarded) ─────────────────────────────────────
    async def set(self, *, tenant: str, mode: str, locked: bool = False, actor_ref: str = "",
                  force: bool = False) -> ModeState:
        """Select the tenant's (or platform's) ONE mode. REPLACES any prior selection — exactly-one, so
        a tenant can never end up with two modes active. Refuses an unknown mode and a locked scope."""
        self._require_known(mode)
        key = self._key(tenant)
        existing = await self._s.get(_COLL, key)
        if existing and existing.get("locked") and not force:
            raise ModeLocked(
                f"payment mode is locked for scope {tenant or 'platform'!r}; pass force=true to change it")
        row = {"id": key, "tenant": tenant, "mode": mode, "locked": bool(locked),
               "set_by": actor_ref, "set_at": self._now()}
        # atomic: ok — single-writer (operator surface only; no hot-path/self-serve writer). Last-write-
        # wins is the right outcome for a config selection, and the put REPLACES the row (exactly-one).
        await self._s.put(_COLL, key, row)
        return self._state(row, "platform" if not tenant else "tenant")

    async def clear(self, *, tenant: str, force: bool = False) -> bool:
        """Drop a selection so the scope resolves to its default/parent again."""
        key = self._key(tenant)
        existing = await self._s.get(_COLL, key)
        if not existing:
            return False
        if existing.get("locked") and not force:
            raise ModeLocked(
                f"payment mode is locked for scope {tenant or 'platform'!r}; pass force=true to clear it")
        return await self._s.delete(_COLL, key)
