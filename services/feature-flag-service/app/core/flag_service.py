"""Core flag logic — CRUD, overrides, cached evaluation, segments, prerequisites,
variants, exposure events, stale cleanup, change audit.

HOT PATH. Flag evaluation runs on every request in the platform, so:
  • definitions are CACHED for FLAG_CACHE_TTL_SECONDS (invalidated on every write),
  • `last_evaluated` is persisted at most once per LAST_SEEN_WRITE_THRESHOLD_SECONDS
    per flag — steady state is ZERO datastore writes per evaluation.

Definitions come from the FlagProvider port; the EVALUATION is always ours, so behaviour
is identical whichever provider you plug in. A provider outage is fail-safe (default OFF).
"""
from __future__ import annotations

import json
from typing import Any

from app.core import evaluate as _eval
from app.core import rollout as _rollout
from app.core import rules as _rules
from app.core.models import Flag, iso, now_ts
from app.core.stores import FlagStore, SegmentStore
from app.ports.flag_provider import ProviderUnavailable

_CACHE_ONE = "flag:"
_CACHE_ALL = "flags:all"


class FlagService:
    def __init__(self, *, store: FlagStore, provider, entitlement, segments: SegmentStore | None = None,
                 cache=None, event_bus=None, salt: str = "", list_limit: int = 500,
                 stale_days: int = 90, cache_ttl: int = 30, last_seen_threshold: int = 3600,
                 exposure_sample_pct: float = 100.0, prereq_max_depth: int = 5) -> None:
        self._store = store
        self._provider = provider
        self._ent = entitlement
        self._segments = segments
        self._cache = cache
        self._bus = event_bus
        self._salt = salt
        self._limit = list_limit
        self._stale_days = stale_days
        self._ttl = cache_ttl
        self._last_seen_threshold = last_seen_threshold
        self._exposure_pct = exposure_sample_pct
        self._prereq_depth = prereq_max_depth

    # ── cache (D1) ─────────────────────────────────────────────────────────────
    async def _cache_raw_get(self, key: str) -> str | None:
        if self._cache is None:
            return None
        try:
            return await self._cache.get(key)
        except Exception:
            return None

    async def _cache_raw_set(self, key: str, value: str, ttl: int) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(key, value, ttl)
        except Exception:
            pass

    async def _cache_get(self, key: str) -> Any | None:
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None                                   # a cache outage never breaks evaluation

    async def _cache_set(self, key: str, value: Any) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(key, json.dumps(value), self._ttl)
        except Exception:
            pass

    async def _invalidate(self, key: str) -> None:
        if self._cache is None:
            return
        for k in (f"{_CACHE_ONE}{key}", _CACHE_ALL):
            try:
                await self._cache.delete(k)
            except Exception:
                pass

    # ── admin CRUD (E6: change audit with actor + before/after) ─────────────────
    async def upsert(self, *, actor: str = "", **kw) -> Flag:
        existing = await self._store.get(kw["key"])
        before = existing.to_dict() if existing else None
        base = before.copy() if before else Flag(key=kw["key"], created=iso()).to_dict()
        base.update({k: v for k, v in kw.items() if v is not None})
        _rollout.validate_variants(base.get("variants") or {})       # raises ValueError
        flag = Flag.from_dict(base)
        await self._store.save(flag)
        await self._invalidate(flag.key)
        await self._emit("flag.changed", {"key": flag.key, "actor": actor,
                                          "before": before, "after": flag.to_dict()})
        return flag

    async def get(self, key: str) -> Flag | None:
        return await self._store.get(key)

    async def list(self, include_archived: bool = False) -> list[dict]:
        return await self._store.list(self._limit, include_archived)

    async def archive(self, key: str, *, actor: str = "") -> Flag | None:
        flag = await self._store.get(key)
        if flag is None:
            return None
        before = flag.to_dict()
        flag.archived = True
        await self._store.save(flag)
        await self._invalidate(key)
        await self._emit("flag.archived", {"key": key, "actor": actor, "before": before})
        return flag

    async def set_override(self, key: str, *, scope: str, subject: str, value: bool,
                           actor: str = "") -> Flag | None:
        flag = await self._store.get(key)
        if flag is None:
            return None
        before = flag.to_dict()
        target = flag.user_overrides if scope == "user" else flag.tenant_overrides
        target[subject] = bool(value)
        await self._store.save(flag)
        await self._invalidate(key)
        await self._emit("flag.changed", {"key": key, "actor": actor,
                                          "before": before, "after": flag.to_dict()})
        return flag

    async def clear_override(self, key: str, *, scope: str, subject: str, actor: str = "") -> Flag | None:
        flag = await self._store.get(key)
        if flag is None:
            return None
        before = flag.to_dict()
        (flag.user_overrides if scope == "user" else flag.tenant_overrides).pop(subject, None)
        await self._store.save(flag)
        await self._invalidate(key)
        await self._emit("flag.changed", {"key": key, "actor": actor,
                                          "before": before, "after": flag.to_dict()})
        return flag

    async def stale(self, days: int | None = None) -> list[dict]:
        cutoff = now_ts() - (days if days is not None else self._stale_days) * 86400
        return await self._store.stale(cutoff, self._limit)

    # ── definitions (cached) ───────────────────────────────────────────────────
    async def _definition(self, key: str) -> dict[str, Any] | None:
        cached = await self._cache_get(f"{_CACHE_ONE}{key}")
        if cached is not None:
            return cached or None                          # {} means "known-missing"
        try:
            flag = await self._provider.get(key)
        except ProviderUnavailable:
            return None                                    # fail-safe → default (OFF)
        await self._cache_set(f"{_CACHE_ONE}{key}", flag or {})
        return flag

    async def _definitions(self) -> list[dict[str, Any]]:
        cached = await self._cache_get(_CACHE_ALL)
        if cached is not None:
            return cached
        try:
            defs = await self._provider.list(self._limit)
        except ProviderUnavailable:
            return []
        await self._cache_set(_CACHE_ALL, defs)
        return defs

    # ── resolvers (segments, prerequisites, entitlement) ───────────────────────
    async def _in_segment(self, flag: dict, attrs: dict) -> bool:
        names = flag.get("segments") or []
        if not names or self._segments is None:
            return False
        for name in names:
            seg = await self._segments.get(name)
            if seg and _rules.first_match(seg.rules, attrs) is not None:
                return True
        return False

    async def _prereqs_ok(self, flag: dict, ctx: dict, depth: int, seen: set[str]) -> bool:
        prereqs = flag.get("prerequisites") or []
        if not prereqs:
            return True
        if depth >= self._prereq_depth or flag.get("key") in seen:
            return False                                   # depth cap / cycle → fail-closed
        seen = seen | {flag.get("key", "")}
        for p in prereqs:
            dep = await self._definition(p.get("key", ""))
            if not dep:
                return False
            r = await self._resolve(dep, ctx, depth + 1, seen)
            if not r["enabled"]:
                return False
            want = p.get("variant")
            if want and r.get("variant") != want:
                return False
        return True

    def _entitled(self, flag: dict, tenant: str) -> bool | None:
        req = flag.get("requires_entitlement")
        if not req:
            return None
        try:
            return bool(self._ent.allowed(tenant=tenant, key=req))
        except Exception:
            return False                                   # fail-closed on a checker error

    async def _resolve(self, flag: dict, ctx: dict, depth: int = 0, seen: set[str] | None = None) -> dict:
        seen = seen or set()
        now = now_ts()
        prereqs_ok = await self._prereqs_ok(flag, ctx, depth, seen)
        entitled = self._entitled(flag, str(ctx.get("tenant") or ""))
        in_seg = await self._in_segment(flag, ctx.get("attrs") or {})
        return _eval.evaluate(flag, ctx, salt=self._salt, entitled=entitled, now=now,
                              prereqs_ok=prereqs_ok, in_segment=in_seg)

    # ── evaluation (hot path) ──────────────────────────────────────────────────
    async def evaluate(self, key: str, ctx: dict[str, Any]) -> dict[str, Any]:
        flag = await self._definition(key)
        result = await self._resolve(flag, ctx) if flag else _eval.evaluate(None, ctx, salt=self._salt)
        if flag:
            await self._after_eval(flag, ctx, result)
        return {"key": key, **result}

    async def evaluate_all(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """One call answers every flag for a request context."""
        out: dict[str, Any] = {}
        for flag in await self._definitions():
            key = flag.get("key", "")
            if not key or flag.get("archived"):
                continue
            result = await self._resolve(flag, ctx)
            out[key] = result
            await self._after_eval(flag, ctx, result)
        return out

    async def _after_eval(self, flag: dict, ctx: dict, result: dict) -> None:
        """Stale bookkeeping (throttled write) + exposure event.

        The `last_evaluated` stamp only feeds the stale-flag report — hour-grain is plenty.
        We throttle with a short-lived cache marker rather than the cached definition's own
        timestamp (which would be stale) and we do NOT invalidate the definition cache,
        because `last_evaluated` plays no part in evaluation. Steady state: 0 writes, 0 reads.
        """
        key = flag.get("key", "")
        if self._cache is not None:
            marker = f"flagseen:{key}"
            if await self._cache_raw_get(marker) is None:
                await self._store.maybe_touch(key, now_ts(), last_evaluated=0.0, threshold_seconds=0)
                await self._cache_raw_set(marker, "1", self._last_seen_threshold)
        else:                                              # no cache → throttle on the stored stamp
            await self._store.maybe_touch(
                key, now_ts(), last_evaluated=float(flag.get("last_evaluated", 0) or 0),
                threshold_seconds=self._last_seen_threshold)
        await self._expose(flag, ctx, result)

    async def _expose(self, flag: dict, ctx: dict, result: dict) -> None:
        """flag.exposed — what makes an A/B experiment measurable. Fire-and-forget, sampled."""
        if self._bus is None or self._exposure_pct <= 0:
            return
        bucket_by = str(flag.get("bucket_by") or "user")
        subject = _rollout.subject_for(bucket_by, ctx)
        if self._exposure_pct < 100:
            if not _rollout.in_rollout(self._salt, f"__exposure__{flag.get('key','')}",
                                       subject, self._exposure_pct):
                return
        try:
            await self._bus.publish("flag.exposed", {
                "key": flag.get("key", ""), "variant": result.get("variant", ""),
                "enabled": result.get("enabled"), "reason": result.get("reason"),
                "subject": subject, "bucket_by": bucket_by,
                "tenant": ctx.get("tenant", ""), "at": iso()})
        except Exception:
            pass                                           # exposure never blocks evaluation

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event, {**payload, "at": iso()})
        except Exception:
            pass                                           # a bus outage never fails an admin write
