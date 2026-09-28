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

import asyncio
import json
from typing import Any

from app.core import evaluate as _eval
from app.core import rollout as _rollout
from app.core import rules as _rules
from app.core.models import Flag, iso, now_ts
from app.core.pii import subject_ref
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
        # CAS-retry: two concurrent upserts/overrides on one flag were last-writer-wins over the WHOLE
        # document (one admin's user-override lost to another's rollout edit). Re-read and re-apply on
        # a lost race so both changes land.
        for _ in range(8):
            existing = await self._store.get(kw["key"])
            before = existing.to_dict() if existing else None
            expected = int(existing.version) if existing else 0
            base = before.copy() if before else Flag(key=kw["key"], created=iso()).to_dict()
            base.update({k: v for k, v in kw.items() if v is not None})
            _rollout.validate_variants(base.get("variants") or {})       # raises ValueError
            flag = Flag.from_dict(base)
            if await self._store.cas_save(flag, expected):
                break
        else:
            raise RuntimeError("flag write contention")
        await self._invalidate(flag.key)
        await self._emit("flag.changed", {"key": flag.key, "actor": actor,
                                          "before": before, "after": flag.to_dict()})
        return flag

    async def get(self, key: str) -> Flag | None:
        return await self._store.get(key)

    async def list(self, include_archived: bool = False) -> list[dict]:
        return await self._store.list(self._limit, include_archived)

    async def _mutate(self, key: str, apply, event: str, actor: str) -> Flag | None:
        """CAS-retry a whole-document flag mutation so concurrent writers don't clobber each other.
        `apply(flag)` mutates the flag in place; re-read and re-apply on a lost race."""
        for _ in range(8):
            flag = await self._store.get(key)
            if flag is None:
                return None
            before = flag.to_dict()
            expected = int(flag.version)
            apply(flag)
            if await self._store.cas_save(flag, expected):
                break
        else:
            raise RuntimeError("flag write contention")
        await self._invalidate(key)
        payload = {"key": key, "actor": actor, "before": before}
        if event == "flag.changed":
            payload["after"] = flag.to_dict()
        await self._emit(event, payload)
        return flag

    async def archive(self, key: str, *, actor: str = "") -> Flag | None:
        def _apply(f):
            f.archived = True
        return await self._mutate(key, _apply, "flag.archived", actor)

    async def set_override(self, key: str, *, scope: str, subject: str, value: bool,
                           actor: str = "") -> Flag | None:
        def _apply(f):
            (f.user_overrides if scope == "user" else f.tenant_overrides)[subject] = bool(value)
        return await self._mutate(key, _apply, "flag.changed", actor)

    async def clear_override(self, key: str, *, scope: str, subject: str, actor: str = "") -> Flag | None:
        def _apply(f):
            (f.user_overrides if scope == "user" else f.tenant_overrides).pop(subject, None)
        return await self._mutate(key, _apply, "flag.changed", actor)

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
    async def _in_segment(self, flag: dict, attrs: dict, memo: dict | None = None) -> bool:
        names = flag.get("segments") or []
        if not names or self._segments is None:
            return False
        # Per-request memo: attrs is constant across one evaluate_all, so a segment's membership is
        # too — compute each segment at most once instead of re-reading + re-matching it per flag.
        seg_memo = memo.setdefault("seg", {}) if memo is not None else None
        for name in names:
            if seg_memo is not None and name in seg_memo:
                member = seg_memo[name]
            else:
                seg = await self._segments.get(name)
                member = bool(seg and _rules.first_match(seg.rules, attrs) is not None)
                if seg_memo is not None:
                    seg_memo[name] = member
            if member:
                return True
        return False

    async def _prereqs_ok(self, flag: dict, ctx: dict, depth: int, seen: set[str],
                          memo: dict | None = None) -> bool:
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
            r = await self._resolve(dep, ctx, depth + 1, seen, memo)
            if not r["enabled"]:
                return False
            want = p.get("variant")
            if want and r.get("variant") != want:
                return False
        return True

    async def _entitled(self, flag: dict, tenant: str, memo: dict | None = None) -> bool | None:
        req = flag.get("requires_entitlement")
        if not req:
            return None
        # FAIL-CLOSED when no real checker is wired: the null driver returns True for everything, so
        # a flag GATED on an entitlement would silently open for every tenant. A declared gate with no
        # enforcement means DENY, not allow.
        if getattr(self._ent, "driver", "") == "null":
            return False
        mkey = (tenant, req)
        ent_memo = memo.setdefault("ent", {}) if memo is not None else None
        if ent_memo is not None and mkey in ent_memo:
            return ent_memo[mkey]                           # one HTTP check per (tenant,key) per call
        try:
            # OFFLOAD: the billing entitlement driver does blocking urllib I/O (a 10s-timeout HTTP
            # call). Called inline on the async evaluation hot path it froze the single-worker event
            # loop for the whole round-trip, stalling every concurrent flag evaluation.
            val = bool(await asyncio.to_thread(self._ent.allowed, tenant=tenant, key=req))
        except Exception:
            val = False                                     # fail-closed on a checker error
        if ent_memo is not None:
            ent_memo[mkey] = val
        return val

    async def _resolve(self, flag: dict, ctx: dict, depth: int = 0, seen: set[str] | None = None,
                       memo: dict | None = None) -> dict:
        seen = seen or set()
        now = now_ts()
        prereqs_ok = await self._prereqs_ok(flag, ctx, depth, seen, memo)
        entitled = await self._entitled(flag, str(ctx.get("tenant") or ""), memo)
        in_seg = await self._in_segment(flag, ctx.get("attrs") or {}, memo)
        return _eval.evaluate(flag, ctx, salt=self._salt, entitled=entitled, now=now,
                              prereqs_ok=prereqs_ok, in_segment=in_seg)

    # ── evaluation (hot path) ──────────────────────────────────────────────────
    async def evaluate(self, key: str, ctx: dict[str, Any]) -> dict[str, Any]:
        flag = await self._definition(key)
        result = await self._resolve(flag, ctx, memo={}) if flag else _eval.evaluate(None, ctx, salt=self._salt)
        if flag:
            await self._after_eval(flag, ctx, result)
        return {"key": key, **result}

    async def evaluate_all(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """One call answers every flag for a request context."""
        out: dict[str, Any] = {}
        memo: dict = {}          # per-request memo: one entitlement check / segment eval per key
        for flag in await self._definitions():
            key = flag.get("key", "")
            if not key or flag.get("archived"):
                continue
            result = await self._resolve(flag, ctx, memo=memo)
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
            # PSEUDONYMISE the subject before broadcasting (feature-flag-006): the raw subject is a
            # user id (global personal data) and flag.exposed fans out to every bus subscriber. The
            # ref is stable per (tenant, subject) so an experiment can still count uniques, without
            # publishing the identity. bucket_by is kept so a consumer knows the ref's domain.
            ref = subject_ref(self._salt, ctx.get("tenant", ""), subject)
            await self._bus.publish("flag.exposed", {
                "key": flag.get("key", ""), "variant": result.get("variant", ""),
                "enabled": result.get("enabled"), "reason": result.get("reason"),
                "subject_ref": ref, "bucket_by": bucket_by,
                "tenant": ctx.get("tenant", ""), "at": iso()})
        except Exception:
            pass                                           # exposure never blocks evaluation

    @staticmethod
    def _redact_flag(d: dict | None) -> dict | None:
        """Replace the raw per-subject override maps with COUNTS before a flag goes on the bus
        (feature-flag-006). user_overrides {user_id: bool} and tenant_overrides {tenant: bool} are
        personal/tenant data; a flag.changed subscriber needs to know they changed, not who is in them."""
        if not d:
            return d
        out = dict(d)
        if "user_overrides" in out:
            out["user_overrides_count"] = len(out.pop("user_overrides") or {})
        if "tenant_overrides" in out:
            out["tenant_overrides_count"] = len(out.pop("tenant_overrides") or {})
        return out

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        if event == "flag.changed":
            payload = {**payload,
                       "before": self._redact_flag(payload.get("before")),
                       "after": self._redact_flag(payload.get("after"))}
        try:
            await self._bus.publish(event, {**payload, "at": iso()})
        except Exception:
            pass                                           # a bus outage never fails an admin write
