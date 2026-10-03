"""Background reconciliation / expiry sweep — the safety net behind the webhook.

A TuwaiqPay webhook is registered by EMAIL, the least reliable delivery there is. When one is
dropped the money is captured but our PaymentRequest stays `pending` forever, because reconcile and
mark_expired are otherwise LAZY — they only run when a redirect hit or an internal call happens to
touch the exact row. This sweep runs them on a schedule instead, so a missed webhook is recovered
without anyone touching the request.

Safety properties (all of them load-bearing — this reaches every tenant's real gateway):
  * BOUNDED — it PAGES the datastore over the pending rows (keyset cursor), never `scan_all`; the
    working set per read is capped by page_size however many pendings accrue.
  * TENANT-SAFE — each row is acted on under its OWN `tenant` (reconcile re-checks row.tenant ==
    tenant, mark_expired touches only that row); the sweep never crosses a tenant boundary.
  * IDEMPOTENT — reconcile and mark_expired share the once-only CAS seam the webhook uses, so a
    re-query can only recover a real settlement, never fabricate or double one; a row a webhook
    completes between two passes simply drops out of the `pending` filter.
  * PER-ROW ISOLATED — one request's error (an unreachable gateway, a malformed row) is logged and
    skipped; it never aborts the pass, so one bad tenant cannot starve the rest.
  * CANCELLABLE — the loop awaits sleep/IO and re-raises CancelledError promptly, so lifespan
    shutdown stops it cleanly.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.core.payment_requests import PENDING, PaymentRequestStore, _COLL
from app.core.payments import PaymentService


@dataclass
class SweepStats:
    """What one pass did — returned for tests/metrics, logged at the end of each pass."""
    scanned: int = 0
    expired: int = 0
    reconciled: int = 0
    errors: int = 0


async def run_sweep_once(
    *,
    datastore,
    requests: PaymentRequestStore,
    service: PaymentService,
    min_age_seconds: int,
    page_size: int,
    concurrency: int = 8,
    now=time.time,
    logger=None,
) -> SweepStats:
    """One full pass over the PENDING requests. Expires the abandoned, re-queries the stale.

    Ordering per row: an already-expired pending is marked EXPIRED (terminal); otherwise, once it is
    older than `min_age_seconds` (well past normal webhook latency, so we only chase genuinely-missed
    ones), its gateway is re-queried through the idempotent reconcile seam.
    """
    stats = SweepStats()
    after: str | None = None
    # payments-019: process a page's rows with BOUNDED CONCURRENCY. reconcile already offloads its
    # blocking gateway fetch off the loop, but awaiting one row at a time made the pass latency the SUM of
    # every row's gateway round-trip — one slow tenant stalled the whole page. A semaphore caps in-flight
    # reconciles so a slow gateway cannot block the rest while keeping the gateway fan-out bounded.
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _process(row: dict) -> None:
        tenant = row.get("tenant", "")
        request_id = row.get("id", "")
        async with sem:
            try:
                if requests.is_expired(row):
                    await requests.mark_expired(row)
                    stats.expired += 1
                elif (now() - float(row.get("created_at", 0))) >= int(min_age_seconds):
                    # Scoped to THIS row's own tenant — reconcile re-checks row.tenant == tenant, so a
                    # forged/mismatched tenant here could only resolve to None, never another shop.
                    await service.reconcile(tenant=tenant, request_id=request_id)
                    stats.reconciled += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad row must never abort the whole sweep
                stats.errors += 1
                if logger is not None:
                    logger.warning(
                        "reconcile_sweep_row_failed",
                        extra={"tenant": tenant, "request_id": request_id},
                        exc_info=True)

    # `where={"status": PENDING}` is a scoped filter (find refuses an empty where), and we page with a
    # keyset cursor on created_at — bounded reads, not a scan. A row that leaves the pending set
    # between pages (completed/expired) simply stops matching; created_at is stable so the cursor never
    # skips or repeats a live row.
    while True:
        rows, after = await datastore.find(
            _COLL, {"status": PENDING}, limit=max(1, int(page_size)),
            after=after, sort=("created_at", "asc"))
        stats.scanned += len(rows)
        await asyncio.gather(*(_process(row) for row in rows))
        if after is None:
            break
    return stats


class ReconcileSweep:
    """Owns the periodic loop + its asyncio task. Start on lifespan startup, stop on shutdown."""

    def __init__(
        self,
        *,
        datastore,
        requests: PaymentRequestStore,
        service: PaymentService,
        interval_seconds: int,
        min_age_seconds: int,
        page_size: int = 200,
        concurrency: int = 8,
        now=time.time,
        logger=None,
    ) -> None:
        self._datastore = datastore
        self._requests = requests
        self._service = service
        self._interval = max(1, int(interval_seconds))
        self._min_age = int(min_age_seconds)
        self._page_size = int(page_size)
        self._concurrency = max(1, int(concurrency))
        self._now = now
        self._logger = logger
        self._task: asyncio.Task | None = None

    async def run_once(self) -> SweepStats:
        return await run_sweep_once(
            datastore=self._datastore, requests=self._requests, service=self._service,
            min_age_seconds=self._min_age, page_size=self._page_size,
            concurrency=self._concurrency, now=self._now, logger=self._logger)

    async def _loop(self) -> None:
        while True:
            try:
                stats = await self.run_once()
                if self._logger is not None:
                    self._logger.info(
                        "reconcile_sweep_pass",
                        extra={"scanned": stats.scanned, "expired": stats.expired,
                               "reconciled": stats.reconciled, "errors": stats.errors})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a whole-pass failure must not kill the loop
                if self._logger is not None:
                    self._logger.warning("reconcile_sweep_pass_failed", exc_info=True)
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def build_sweep(app, settings, logger=None) -> ReconcileSweep:
    """Assemble the sweep from the app's container + settings, mirroring build_payment_service's wiring
    (one request store, one service reaching each tenant's own gateway) without needing a Request."""
    import types

    from app.api.payments_deps import build_payment_service

    container = app.state.container
    # build_payment_service reads request.app.state.container — a shim gives it exactly that, so the
    # sweep's service is wired identically to the request path (no second, divergent construction).
    shim = types.SimpleNamespace(app=app)
    return ReconcileSweep(
        datastore=container.datastore,
        requests=PaymentRequestStore(container.datastore),
        service=build_payment_service(shim, settings),
        interval_seconds=settings.PAYMENTS_RECONCILE_INTERVAL_SECONDS,
        min_age_seconds=settings.PAYMENTS_RECONCILE_MIN_AGE_SECONDS,
        page_size=settings.PAYMENTS_RECONCILE_PAGE_SIZE,
        concurrency=getattr(settings, "PAYMENTS_RECONCILE_CONCURRENCY", 8),
        logger=logger)
