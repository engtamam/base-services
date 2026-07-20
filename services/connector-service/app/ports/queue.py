"""Queue port — a job survives the worker that was holding it.

WHAT THIS USED TO BE, AND WHY IT LOST WORK.

    async def dequeue(self, queue: str) -> Job | None:
        "Pop the next job, or None if the queue is empty."

Pop. The Redis adapter was `LPOP`, so the job left the queue the instant a worker read it, and
everything after that point ran with no record anywhere that the work existed. A worker killed
mid-scan — an OOM, a node drain, a rolling deploy — took its job with it. Nothing retried it,
nothing failed, nothing logged: the queue was empty and every counter agreed.

The old docstring said "at-least-once semantics for real backends". At-least-once is not a
property a backend can supply on its own — it needs a way for the consumer to say *I have
finished*, and this interface had none. So the guarantee was stated in prose and made impossible
by the signature, which is the worse of the two failures: an adapter author reads the docstring,
believes the contract is handled elsewhere, and implements the pop.

WHAT A LEASE CHANGES. `dequeue` now HIDES a job for `lease_seconds` instead of removing it, and
returns a token proving who holds it. `ack` deletes it for good. `reclaim` returns anything whose
lease expired. A worker that dies simply stops acking, and the job comes back on its own.

THE COST IS DUPLICATES, AND THAT IS THE RIGHT TRADE. A job whose worker died after finishing but
before acking will run twice. At-least-once is what a queue can honestly offer; exactly-once needs
the *consumer* to be idempotent, which is a property of the work and not of the transport. Both
real consumers here are: a virus scan recomputes the same verdict, an export rebuilds the same
clip. Silently losing an uploaded video is not recoverable. Scanning it twice is.

`lease_seconds` IS REQUIRED, with no default. A default would be a timeout invented by this file
for work it knows nothing about: too short and a slow scan is reclaimed while it is still running,
creating exactly the duplicate work the caller was trying to bound; too long and a dead worker's
job is stuck until it elapses. Only the caller knows how long its work takes, and it reads that
from config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Job:
    id: str
    queue: str
    payload: dict[str, Any]
    # Proof that THIS dequeue holds the job. A worker whose lease expired and was reclaimed by
    # somebody else may still be running and still holding a Job object; its token no longer
    # matches, so its ack is refused rather than deleting a job the new holder is working on.
    token: str = field(default="")


@runtime_checkable
class Queue(Protocol):
    driver: str

    async def enqueue(self, queue: str, payload: dict[str, Any]) -> str:
        """Enqueue a job; return its id."""
        ...

    async def dequeue(self, queue: str, *, lease_seconds: int) -> Job | None:
        """Lease the next job for `lease_seconds`, or None if nothing is ready.

        The job is HIDDEN, not removed. It comes back — via `reclaim` — unless it is acked first.
        """
        ...

    async def ack(self, job: Job) -> bool:
        """Delete a leased job for good. False if the lease was lost (someone else holds it now)."""
        ...

    async def nack(self, job: Job) -> bool:
        """Return a leased job to the queue now, for a worker that knows it failed.

        Without this a known failure waits out the whole lease before anyone retries it, turning a
        fast, loud error into a slow, silent one.
        """
        ...

    async def reclaim(self, queue: str) -> int:
        """Return every expired lease to the queue. Called by a sweeper. Returns how many."""
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
