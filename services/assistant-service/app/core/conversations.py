"""Conversations — what a person asked, kept only as long as it is useful.

A help conversation is personal data twice over. It carries what the user typed, which is often the
identifiers the platform works hard to keep out of its logs ("why was invoice 8891 for +9665… not
paid?"), and it carries what they did not understand, which is its own kind of exposure. So this
store is built around three rules and none of them are optional.

SCOPED BY TENANT AND SUBJECT, AT THE STORE. Every read filters on both, and the filter is applied
in the query rather than checked in a handler afterwards. A handler check is one forgotten `if`
away from a cross-tenant read; a query that cannot express the wrong answer is not.

THE SUBJECT IS A PSEUDONYM. Rows carry `subject_ref`, never the identity. Erasure then destroys the
linkage rather than hunting for rows by name, and a stolen database dump does not say who asked
what.

ENCRYPTED AT REST, AND IT REFUSES TO PRETEND. If encryption is configured on and the key is
missing, this raises rather than writing plaintext. Storing plaintext while reporting success is
the failure that looks fine for a year and is discovered by someone else.
"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from app.core.pii import subject_ref
from app.ports.errors import ConfigError

_CONVERSATIONS = "assistant_conversations"
_MESSAGES = "assistant_messages"

USER, ASSISTANT = "user", "assistant"

# Format marker. Versioned from the first line so a later algorithm change can be rolled out by
# reading both — an unversioned ciphertext is a migration nobody can perform.
_PREFIX = "v1:gcm:"


class MessageCrypto:
    """AES-256-GCM over message content.

    The key is derived once at construction rather than per message: scrypt is deliberately
    expensive, and re-deriving it on every read turns a conversation list into a CPU load test.
    """

    def __init__(self, secret: str, *, required: bool) -> None:
        self._required = required
        self._key: bytes | None = None
        if secret:
            # A per-deployment salt would be better still, but it must then be stored and rotated
            # with the key; the secret itself is the entropy that matters here.
            self._key = hashlib.scrypt(secret.encode("utf-8"), salt=b"assistant-message-v1",
                                       n=2**14, r=8, p=1, dklen=32)
        elif required:
            raise ConfigError(
                "ASSISTANT_ENCRYPT_MESSAGES is on but ASSISTANT_MESSAGE_KEY is empty — refusing "
                "to store conversation content in plaintext while reporting success")

    @property
    def enabled(self) -> bool:
        return self._key is not None

    def encrypt(self, text: str) -> str:
        if self._key is None:
            return text
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        iv = os.urandom(12)
        ct = AESGCM(self._key).encrypt(iv, text.encode("utf-8"), None)
        return _PREFIX + base64.b64encode(iv + ct).decode("ascii")

    def decrypt(self, stored: str) -> str:
        if not stored.startswith(_PREFIX):
            return stored          # written before encryption was switched on
        if self._key is None:
            # Loud. Returning "" here would silently empty every historical message the moment a
            # key went missing, and look like the data had never been there.
            raise ConfigError("encrypted conversation content found but no message key is "
                              "configured — the history cannot be read without it")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        blob = base64.b64decode(stored[len(_PREFIX):])
        return AESGCM(self._key).decrypt(blob[:12], blob[12:], None).decode("utf-8")


@dataclass(frozen=True)
class Message:
    id: str
    conversation_id: str
    role: str
    content: str
    created: str
    citations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Conversation:
    id: str
    tenant: str
    subject_ref: str
    title: str
    surface: str = ""
    created: str = ""
    updated: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ConversationStore:
    def __init__(self, store, *, pii_salt: str, crypto: MessageCrypto, now, new_id) -> None:
        self._s = store
        self._salt = pii_salt
        self._crypto = crypto
        self._now = now
        self._new_id = new_id

    def _ref(self, tenant: str, subject: str) -> str:
        return subject_ref(self._salt, tenant, subject)

    # ── write ─────────────────────────────────────────────────────────────────
    async def start(self, *, tenant: str, subject: str, title: str, surface: str = "") -> Conversation:
        ref = self._ref(tenant, subject)
        now = self._now()
        conv = Conversation(id=self._new_id(), tenant=tenant, subject_ref=ref,
                            title=title[:120], surface=surface, created=now, updated=now)
        await self._s.put(_CONVERSATIONS, conv.id, {
            "id": conv.id, "tenant": tenant, "subject_ref": ref, "title": conv.title,
            "surface": surface, "created": now, "updated": now})
        return conv

    async def add(self, *, conversation_id: str, tenant: str, subject: str, role: str,
                  content: str, citations: tuple[str, ...] = ()) -> Message:
        # Ownership is re-established before every write. Trusting a conversation id supplied by a
        # caller would let anyone append to — and therefore read the context of — someone else's.
        conv = await self.get(conversation_id=conversation_id, tenant=tenant, subject=subject)
        if conv is None:
            raise PermissionError("conversation not found for this tenant and subject")
        now = self._now()
        msg_id = self._new_id()
        await self._s.put(_MESSAGES, msg_id, {
            "id": msg_id, "conversation_id": conversation_id, "tenant": tenant,
            "subject_ref": conv.subject_ref, "role": role,
            "content": self._crypto.encrypt(content),
            "citations": list(citations), "created": now})
        await self._s.put(_CONVERSATIONS, conv.id, {
            "id": conv.id, "tenant": conv.tenant, "subject_ref": conv.subject_ref,
            "title": conv.title, "surface": conv.surface, "created": conv.created, "updated": now})
        return Message(id=msg_id, conversation_id=conversation_id, role=role, content=content,
                       created=now, citations=citations)

    # ── read ──────────────────────────────────────────────────────────────────
    async def get(self, *, conversation_id: str, tenant: str, subject: str) -> Conversation | None:
        row = await self._s.get(_CONVERSATIONS, conversation_id)
        if not row:
            return None
        # Checking only the id would make a conversation id a bearer token, so ownership is
        # re-established here on every read.
        #
        # THE TENANT COMPARISON IS REDUNDANT TODAY, and stays deliberately. `subject_ref` is
        # HMAC(salt, tenant|subject), so a different tenant already yields a different pseudonym
        # and the ref check alone blocks a cross-tenant read — mutation testing confirmed removing
        # the tenant clause fails nothing. It remains because that safety is a property of the
        # PSEUDONYM SCHEME rather than of this function: if subject_ref ever became
        # tenant-independent, this line is what would still stand between two tenants.
        if row.get("tenant") != tenant or row.get("subject_ref") != self._ref(tenant, subject):
            return None
        return _conv(row)

    async def list(self, *, tenant: str, subject: str, limit: int = 20,
                   after: str | None = None) -> tuple[list[Conversation], str | None]:
        """Paginated. Unbounded listing is how a store that worked in year one stops responding in
        year two, and the person it stops responding for is the heaviest user."""
        rows, cursor = await self._s.find(
            _CONVERSATIONS, {"tenant": tenant, "subject_ref": self._ref(tenant, subject)},
            limit=limit, after=after, sort=("updated", "desc"))
        return [_conv(r) for r in rows], cursor

    async def messages(self, *, conversation_id: str, tenant: str, subject: str,
                       limit: int = 100, after: str | None = None) -> tuple[list[Message], str | None]:
        if await self.get(conversation_id=conversation_id, tenant=tenant, subject=subject) is None:
            return [], None
        rows, cursor = await self._s.find(
            _MESSAGES, {"tenant": tenant, "conversation_id": conversation_id},
            limit=limit, after=after, sort=("created", "asc"))
        return [Message(id=r["id"], conversation_id=r["conversation_id"], role=r.get("role", USER),
                        content=self._crypto.decrypt(r.get("content", "")),
                        created=r.get("created", ""),
                        citations=tuple(r.get("citations") or ())) for r in rows], cursor

    # ── delete ────────────────────────────────────────────────────────────────
    async def delete(self, *, conversation_id: str, tenant: str, subject: str) -> bool:
        if await self.get(conversation_id=conversation_id, tenant=tenant, subject=subject) is None:
            return False
        await self._purge_messages(tenant=tenant, conversation_id=conversation_id)
        await self._s.delete(_CONVERSATIONS, conversation_id)
        return True

    async def _purge_messages(self, *, tenant: str, conversation_id: str) -> int:
        deleted, cursor = 0, None
        while True:
            rows, cursor = await self._s.find(
                _MESSAGES, {"tenant": tenant, "conversation_id": conversation_id},
                limit=500, after=cursor, sort=("created", "asc"))
            for r in rows:
                await self._s.delete(_MESSAGES, r["id"])
                deleted += 1
            if not cursor:
                break
        return deleted

    async def erase_subject(self, *, tenant: str, subject: str) -> dict[str, int]:
        """Everything this person asked, gone. Used by the DSAR fan-out.

        Messages are deleted before their conversation: the other order can strand messages whose
        parent is already gone, and a stranded message is content nobody can find to erase.
        """
        ref = self._ref(tenant, subject)
        convs, msgs, cursor = 0, 0, None
        while True:
            rows, cursor = await self._s.find(_CONVERSATIONS, {"tenant": tenant, "subject_ref": ref},
                                              limit=500, after=cursor, sort=("created", "asc"))
            for r in rows:
                msgs += await self._purge_messages(tenant=tenant, conversation_id=r["id"])
                await self._s.delete(_CONVERSATIONS, r["id"])
                convs += 1
            if not cursor:
                break
        return {"conversations": convs, "messages": msgs}


    async def enforce_retention(self, *, cutoff_iso: str, limit: int = 500) -> dict[str, int]:
        """Delete conversations last touched before `cutoff_iso`.

        A retention policy that is stored, validated and surfaced but never executed is the most
        common compliance defect there is: every review passes because the setting exists, and the
        data is still there years later. This is why the sweeper exists and why it has a caller.

        Bounded per run so one enormous backlog degrades into several runs rather than one run that
        never finishes and is killed halfway through, leaving messages whose conversation is gone.
        """
        # sweeper: retention crosses EVERY tenant by definition, so there is no `where` that
        # narrows it — a per-tenant filter would need a tenant list this service does not have and
        # would silently skip any tenant missing from it. Bounded by `limit` so one large backlog
        # degrades into several runs rather than one run that is killed halfway, leaving messages
        # whose conversation is already gone. This is a cron caller, never a request path.
        rows = await self._s.scan_all(_CONVERSATIONS, limit)
        convs, msgs = 0, 0
        for r in rows:
            # ISO-8601 UTC compares correctly as a string, which is why it is stored that way.
            if (r.get("updated") or "") >= cutoff_iso:
                continue
            msgs += await self._purge_messages(tenant=r.get("tenant", ""),
                                               conversation_id=r["id"])
            await self._s.delete(_CONVERSATIONS, r["id"])
            convs += 1
        return {"conversations": convs, "messages": msgs}


def _conv(row: dict) -> Conversation:
    return Conversation(id=row["id"], tenant=row.get("tenant", ""),
                        subject_ref=row.get("subject_ref", ""), title=row.get("title", ""),
                        surface=row.get("surface", ""), created=row.get("created", ""),
                        updated=row.get("updated", ""))
