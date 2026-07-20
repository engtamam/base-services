"""Redis Queue — infra-gated, lazy import. List-backed FIFO with visibility leases.

WHY `LMOVE` AND NOT `LPOP`. The old adapter popped, so the moment between "the worker has the job"
and "the worker finished" was a window in which the job existed only inside one process. A crash
there lost it silently — the queue was empty and every counter agreed.

`LMOVE ready -> inflight` is one atomic step: the job is never in neither list, so there is no
instant at which a crash can lose it. It leaves the ready list and appears in `inflight`, where a
sweeper can find it again.

THE SECOND WINDOW IS REAL AND HANDLED. Recording the lease deadline is a separate command from the
LMOVE, so a crash BETWEEN them leaves a job in `inflight` with no deadline. That entry would
otherwise sit there forever — invisible, undelivered, and not expired, because it has no expiry to
exceed. `reclaim` therefore treats a missing deadline as already expired rather than skipping what
it cannot classify: an unmeasurable lease is not an infinite one.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.adapters.lazy import load
from app.ports.errors import ConfigError
from app.ports.queue import Job


class RedisQueue:
    driver = "redis"

    def __init__(self, url: str) -> None:
        if not url:
            raise ConfigError("QUEUE_URL is required for the redis driver")
        redis = load("redis.asyncio", "redis")
        self._client: Any = redis.from_url(url, decode_responses=True)

    def _k(self, queue: str) -> str:
        return f"queue:{queue}"

    def _inflight(self, queue: str) -> str:
        return f"queue:{queue}:inflight"

    def _leases(self, queue: str) -> str:
        return f"queue:{queue}:leases"

    async def enqueue(self, queue: str, payload: dict[str, Any]) -> str:
        job = Job(id=uuid.uuid4().hex, queue=queue, payload=dict(payload))
        await self._client.rpush(self._k(queue),
                                 json.dumps({"id": job.id, "payload": job.payload}))
        return job.id

    async def dequeue(self, queue: str, *, lease_seconds: int) -> Job | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive — a zero lease is a pop, which is "
                             "the bug this port exists to prevent")
        raw = await self._client.lmove(self._k(queue), self._inflight(queue), "LEFT", "RIGHT")
        if raw is None:
            return None
        data = json.loads(raw)
        token = uuid.uuid4().hex
        # Keyed by the RAW envelope, because that is what LREM needs to remove the exact entry.
        await self._client.hset(self._leases(queue), raw,
                                json.dumps({"token": token,
                                            "expires": time.time() + lease_seconds}))
        return Job(id=data["id"], queue=queue, payload=data["payload"], token=token)

    async def _release(self, job: Job, *, requeue: bool) -> bool:
        queue = job.queue
        raw = json.dumps({"id": job.id, "payload": job.payload})
        held = await self._client.hget(self._leases(queue), raw)
        if held is None:
            return False
        # Token equality, not mere presence: a worker whose lease expired and was reclaimed by
        # somebody else must not delete the job the new holder is working on.
        if json.loads(held).get("token") != job.token:
            return False
        await self._client.lrem(self._inflight(queue), 1, raw)
        await self._client.hdel(self._leases(queue), raw)
        if requeue:
            # To the FRONT: a known failure retries before newer work, so a poison message
            # surfaces immediately instead of hiding behind a busy queue.
            await self._client.lpush(self._k(queue), raw)
        return True

    async def ack(self, job: Job) -> bool:
        return await self._release(job, requeue=False)

    async def nack(self, job: Job) -> bool:
        return await self._release(job, requeue=True)

    async def reclaim(self, queue: str) -> int:
        now = time.time()
        reclaimed = 0
        for raw in await self._client.lrange(self._inflight(queue), 0, -1):
            held = await self._client.hget(self._leases(queue), raw)
            # A missing lease means the process died between the LMOVE and the HSET. Treated as
            # expired: an entry we cannot measure is not one to leave in flight forever.
            if held is not None and json.loads(held).get("expires", 0) > now:
                continue
            if await self._client.lrem(self._inflight(queue), 1, raw):
                await self._client.hdel(self._leases(queue), raw)
                await self._client.rpush(self._k(queue), raw)
                reclaimed += 1
        return reclaimed

    async def health(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()
