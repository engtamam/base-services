"""Per-tenant, per-option commerce toggles an operator owns — enable/disable, with a hard lock.

The commerce plan gives each shop independently-lockable options. This service gates the NATIVE
in-WhatsApp pay option (order_details "Review and Pay"). It is a SECOND gate on top of the global,
region-level `WHATSAPP_NATIVE_PAY_ENABLED` switch: the global flag says "native pay is available in
this deployment at all" (KSA-dormant until Meta enables it); this gate says "and this specific shop
may use it". Both must be true.

WHY NOT RuntimeConfig. RuntimeConfig overrides a Settings knob with tenant-then-platform precedence
(the tenant value wins), right for a per-tenant preference. An operator LOCK is the opposite: the
platform must be able to force an option OFF (fraud, compliance) so no per-tenant setting turns it
back on. So this is its own primitive with platform-wins-when-locked precedence and a `locked` flag.

WHAT `locked` MEANS. Enforcement reads only `enabled` — `is_enabled()` is the one call the route
makes. `locked` guards WRITES: a locked scope refuses a change (and a clear) unless the caller passes
`force`, so a careless re-toggle cannot silently undo a deliberate lock, and a self-serve UI can read
`locked` to grey the toggle out. A PLATFORM ("") lock is the fleet-wide hard override; a TENANT lock
freezes just that shop's row.

An UNKNOWN option is always refused — a typo must never read as "enabled".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# option -> default `enabled` when no override row exists. Native pay defaults OFF: it is build-ahead
# and KSA-dormant, so a shop is opted IN deliberately, never by default.
KNOWN_OPTIONS: dict[str, bool] = {
    "native_pay": False,
}

_COLL = "commerce_option_gates"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class OptionGateError(ValueError):
    """An option toggle the caller may not perform, with a reason they can act on."""


class UnknownOption(OptionGateError):
    """An option this service does not gate — refused so a typo never reads as enabled."""


class OptionLocked(OptionGateError):
    """A change to a locked scope, without force."""


@dataclass(frozen=True)
class OptionState:
    option: str
    enabled: bool
    locked: bool
    source: str           # "default" (no row) | "override" (a stored row decided it)
    set_by: str = ""
    set_at: str = ""


class OptionGate:
    def __init__(self, store, *, known: dict[str, bool] | None = None, now=_utc_now) -> None:
        self._s = store
        self._known = dict(known if known is not None else KNOWN_OPTIONS)
        self._now = now

    @staticmethod
    def _key(tenant: str, option: str) -> str:
        return f"{tenant or '*'}|{option}"

    def _require_known(self, option: str) -> bool:
        if option not in self._known:
            raise UnknownOption(
                f"unknown commerce option {option!r}; known: {sorted(self._known)}")
        return self._known[option]

    @staticmethod
    def _state(option: str, row: dict) -> OptionState:
        return OptionState(option=option, enabled=bool(row.get("enabled")),
                           locked=bool(row.get("locked")), source="override",
                           set_by=row.get("set_by", ""), set_at=row.get("set_at", ""))

    # ── read (the enforcement path) ─────────────────────────────────────────────
    async def is_enabled(self, tenant: str, option: str) -> bool:
        return (await self.status(tenant=tenant, option=option)).enabled

    async def status(self, *, tenant: str, option: str) -> OptionState:
        default = self._require_known(option)
        # 1) a PLATFORM lock is the fleet-wide hard override — it wins over any tenant row.
        plat = await self._s.get(_COLL, self._key("", option))
        if plat and plat.get("locked"):
            return self._state(option, plat)
        # 2) an explicit tenant row (locked or not) decides that shop.
        if tenant:
            row = await self._s.get(_COLL, self._key(tenant, option))
            if row:
                return self._state(option, row)
        # 3) a platform default row (unlocked) applies to shops with no row of their own.
        if plat:
            return self._state(option, plat)
        # 4) the built-in default.
        return OptionState(option=option, enabled=default, locked=False, source="default")

    async def list(self, *, tenant: str) -> list[OptionState]:
        return [await self.status(tenant=tenant, option=o) for o in sorted(self._known)]

    # ── write (operator-only, lock-guarded) ─────────────────────────────────────
    async def set(self, *, tenant: str, option: str, enabled: bool, locked: bool = False,
                  actor_ref: str = "", force: bool = False) -> OptionState:
        self._require_known(option)
        key = self._key(tenant, option)
        existing = await self._s.get(_COLL, key)
        if existing and existing.get("locked") and not force:
            raise OptionLocked(
                f"option {option!r} is locked for scope {tenant or 'platform'!r}; pass force=true to "
                f"change a locked option")
        row = {"id": key, "tenant": tenant, "option": option, "enabled": bool(enabled),
               "locked": bool(locked), "set_by": actor_ref, "set_at": self._now()}
        # atomic: ok — single-writer (the operator surface only; no hot-path or self-serve writer).
        # The get-then-put lock guard is a convenience, not a money invariant: two racing OPERATOR
        # toggles resolve last-write-wins, which is the right outcome for a config flag.
        await self._s.put(_COLL, key, row)
        return self._state(option, row)

    async def clear(self, *, tenant: str, option: str, force: bool = False) -> bool:
        """Drop an override so the option resolves to its default again."""
        self._require_known(option)
        key = self._key(tenant, option)
        existing = await self._s.get(_COLL, key)
        if not existing:
            return False
        if existing.get("locked") and not force:
            raise OptionLocked(
                f"option {option!r} is locked for scope {tenant or 'platform'!r}; pass force=true to "
                f"clear a locked option")
        return await self._s.delete(_COLL, key)
