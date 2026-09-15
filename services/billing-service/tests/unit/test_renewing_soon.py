"""SubscriptionStore.renewing_soon — the read-only pre-renewal horizon query.

It must: include exactly the active/trialing subscriptions whose period ends at or before the horizon
(boundary-exact), exclude cancelled/paused/one-time/zero-end rows, page a large set via a real cursor
without dropping or duplicating a row, and never mutate anything.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import SubscriptionStore

NOW = 1_000_000.0
HORIZON = NOW + 172_800          # +2 days


def run(c):
    return asyncio.run(c)


def _store(rows):
    ds = MemoryDataStore()
    for r in rows:
        run(ds.put("subscriptions", r["id"], r))
    return SubscriptionStore(ds, None)


def _sub(id, *, pe, status="active", interval="month", tenant="t", created=None):
    return {"id": id, "tenant": tenant, "plan_id": "pro", "status": status, "interval": interval,
            "current_period_end": pe, "limits_snapshot": {"k": 1},
            "created": created or f"2026-01-01T00:00:{int(id[-2:]) if id[-2:].isdigit() else 0:02d}"}


def test_includes_within_horizon_excludes_beyond():
    st = _store([
        _sub("in_past", pe=NOW - 10),          # already lapsed (<= horizon) — included
        _sub("in_soon", pe=NOW + 100),         # within 2 days — included
        _sub("in_edge", pe=HORIZON),           # EXACTLY at the horizon — included
        _sub("out_far", pe=HORIZON + 1),       # one second past — excluded
    ])
    rows, _ = run(st.renewing_soon(HORIZON, limit=100))
    assert {r["id"] for r in rows} == {"in_past", "in_soon", "in_edge"}


def test_excludes_non_renewing_statuses_and_once_and_zero():
    st = _store([
        _sub("active_ok", pe=NOW + 100, status="active"),
        _sub("trialing_ok", pe=NOW + 100, status="trialing"),
        _sub("canceled", pe=NOW + 100, status="canceled"),
        _sub("paused", pe=NOW + 100, status="paused"),
        _sub("once", pe=NOW + 100, interval="once"),      # one-time purchase never renews
        _sub("zero_end", pe=0.0),                          # no period end (never lapses)
    ])
    rows, _ = run(st.renewing_soon(HORIZON, limit=100))
    assert {r["id"] for r in rows} == {"active_ok", "trialing_ok"}


def test_spans_all_tenants():
    st = _store([
        _sub("a", pe=NOW + 100, tenant="tenant_a"),
        _sub("b", pe=NOW + 100, tenant="tenant_b"),
    ])
    rows, _ = run(st.renewing_soon(HORIZON, limit=100))
    assert {r["tenant"] for r in rows} == {"tenant_a", "tenant_b"}   # not one tenant


def test_cursor_pages_the_whole_set_without_drops_or_dups():
    # 25 matching subs; page in batches of 10 until the cursor is exhausted.
    st = _store([_sub(f"s{i:02d}", pe=NOW + 100) for i in range(25)])
    seen, cursor, pages = [], None, 0
    while True:
        rows, cursor = run(st.renewing_soon(HORIZON, limit=10, cursor=cursor))
        seen.extend(r["id"] for r in rows)
        pages += 1
        if not cursor:
            break
        assert pages < 20                                 # guard against a non-terminating cursor
    assert len(seen) == len(set(seen)) == 25              # every row once, none dropped, none doubled


def test_a_page_may_return_fewer_than_limit_but_still_a_cursor():
    # A window of rows where only a few match: one find() of `limit` rows yields few matches yet a
    # cursor to continue — the caller must page until the cursor is empty.
    rows = [_sub(f"m{i:02d}", pe=NOW + 100) for i in range(3)]
    rows += [_sub(f"x{i:02d}", pe=HORIZON + 10_000) for i in range(30)]   # out of horizon
    st = _store(rows)
    got, cursor, guard = [], None, 0
    while True:
        page, cursor = run(st.renewing_soon(HORIZON, limit=10, cursor=cursor))
        got.extend(r["id"] for r in page)
        guard += 1
        if not cursor or guard > 20:
            break
    assert {g for g in got} == {"m00", "m01", "m02"}       # only the in-horizon ones, across pages
