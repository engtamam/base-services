"""UploadSession — the state of one LARGE, direct-to-bucket upload while it is in flight.

A small upload is atomic: bytes in, scanned, stored, done, in a single request. A large upload is a
CONVERSATION — initiate, the client PUTs parts straight to the bucket, complete, then an async scan —
so its state has to live somewhere between requests. That somewhere is an UploadSession.

The state machine is deliberately small and one-directional; a session never goes backwards:

    pending      multipart created, presigned URLs handed out, awaiting the client's `complete`
    scanning     client completed; the object is assembled in the bucket; hash+scan running
    ready        clean — a FileObject now exists and the blob is ref-counted
    quarantined  the declared sha did not match the bytes, or the scanner rejected them
    aborted      swept (never completed within its TTL) or deleted before completion

The blob is stored at `storage_key = declared_sha256` (content-addressed, same as the small path),
but that key is only TRUSTED — ref-counted, dedup-eligible, downloadable — once the session reaches
`ready`. Until then the bytes exist but no FileObject references them.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any

PENDING = "pending"
SCANNING = "scanning"
READY = "ready"
QUARANTINED = "quarantined"
ABORTED = "aborted"

_TERMINAL = frozenset({READY, QUARANTINED, ABORTED})


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class UploadSession:
    id: str
    tenant: str
    owner: str
    filename: str
    content_type: str
    declared_sha256: str
    declared_size: int
    upload_id: str            # the store's multipart id
    part_size: int
    part_count: int
    status: str = PENDING
    file_id: str = ""         # set when it reaches READY
    reason: str = ""          # why it was quarantined/aborted
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)

    @property
    def content_key(self) -> str:
        return self.declared_sha256   # the FINAL content-addressed key, written only after verify

    @property
    def staging_key(self) -> str:
        # The client uploads HERE first. Per-session, so a lying client can only corrupt its own
        # staging object — never a shared content blob. Promoted to content_key after hash+scan.
        return f"staging/{self.id}"

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "tenant": self.tenant, "owner": self.owner,
            "filename": self.filename, "content_type": self.content_type,
            "declared_sha256": self.declared_sha256, "declared_size": self.declared_size,
            "upload_id": self.upload_id, "part_size": self.part_size,
            "part_count": self.part_count, "status": self.status, "file_id": self.file_id,
            "reason": self.reason, "created": self.created, "updated": self.updated,
        }

    def public_dict(self) -> dict[str, Any]:
        """What a client is allowed to see about its own session — never the internal upload_id."""
        return {
            "id": self.id, "status": self.status, "filename": self.filename,
            "content_type": self.content_type, "size": self.declared_size,
            "sha256": self.declared_sha256, "file_id": self.file_id,
            "reason": self.reason, "created": self.created, "updated": self.updated,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UploadSession":
        return cls(
            id=d["id"], tenant=d.get("tenant", ""), owner=d.get("owner", ""),
            filename=d["filename"], content_type=d["content_type"],
            declared_sha256=d["declared_sha256"], declared_size=int(d["declared_size"]),
            upload_id=d["upload_id"], part_size=int(d["part_size"]),
            part_count=int(d["part_count"]), status=d.get("status", PENDING),
            file_id=d.get("file_id", ""), reason=d.get("reason", ""),
            created=d.get("created", _now()), updated=d.get("updated", _now()),
        )


_SESSIONS = "upload_sessions"


class UploadSessionStore:
    """UploadSessions over DataStore. Listing/sweeping is keyset-paged and pushed into the store —
    a sweeper must never scan the whole table (the [[datastore-find-discipline]] rule)."""

    def __init__(self, store) -> None:
        self._store = store

    async def create(self, sess: UploadSession) -> UploadSession:
        await self._store.put(_SESSIONS, sess.id, sess.to_dict())
        return sess

    async def get(self, session_id: str) -> UploadSession | None:
        row = await self._store.get(_SESSIONS, session_id)
        return UploadSession.from_dict(row) if row else None

    async def save(self, sess: UploadSession) -> UploadSession:
        sess.updated = _now()
        await self._store.put(_SESSIONS, sess.id, sess.to_dict())
        return sess

    async def delete(self, session_id: str) -> None:
        await self._store.delete(_SESSIONS, session_id)

    async def list_for_tenant(self, tenant: str, limit: int = 100,
                              after: str | None = None) -> tuple[list[dict], str | None]:
        return await self._store.find(_SESSIONS, {"tenant": tenant}, limit=limit, after=after,
                                      sort=("created", "desc"))

    async def find_stale_pending(self, before_iso: str, limit: int = 100,
                                 after: str | None = None) -> tuple[list[dict], str | None]:
        """Pending/scanning sessions older than `before_iso` — the sweeper's page. Filter and bound
        are pushed into the store; the cost is the page, not the table."""
        # BOTH pending and scanning: a session stuck in SCANNING (its scan-queue job lost, e.g. a
        # finalize crash) would otherwise never be released by this sweeper, orphaning it.
        rows, cursor = await self._store.find(_SESSIONS, {"status": [PENDING, SCANNING]}, limit=limit,
                                              after=after, sort=("created", "asc"))
        stale = [r for r in rows if r.get("created", "") < before_iso]
        return stale, cursor
