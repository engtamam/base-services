"""Domain model — User. Kept generic (no product-specific fields)."""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class User:
    id: str
    tenant: str
    username: str
    password_hash: str
    caps: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    status: str = "active"          # active | disabled
    # Has the human behind this address PROVEN they can read it? Separate from `status` on
    # purpose: "an admin disabled you" and "you never clicked the link" are different facts with
    # different remedies, and folding them into one field loses which one applies.
    #
    # THREE-VALUED, and the third value is the point. None means the row predates verification —
    # nobody ever asked this account to prove anything. False means we asked and they have not.
    # Collapsing them would force one wrong answer at upgrade: either every existing user is
    # locked out, or every future one is waved through. The policy decides; the model records.
    email_verified: bool | None = None
    mfa_secret: str | None = None   # set when TOTP enrolled
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "tenant": self.tenant, "username": self.username,
            "password_hash": self.password_hash, "caps": self.caps, "roles": self.roles,
            "status": self.status, "mfa_secret": self.mfa_secret,
            "email_verified": self.email_verified,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "User":
        return cls(
            id=d["id"], tenant=d["tenant"], username=d["username"],
            password_hash=d["password_hash"], caps=list(d.get("caps") or []),
            roles=list(d.get("roles") or []), status=d.get("status", "active"),
            mfa_secret=d.get("mfa_secret"),
            # .get() with no default: an ABSENT key stays None (a legacy row), which is not the
            # same as a stored False. See the field comment.
            email_verified=d.get("email_verified"),
            created_at=d.get("created_at", _now()), updated_at=d.get("updated_at", _now()),
        )

    def public(self) -> dict[str, Any]:
        """Safe projection — NEVER exposes password_hash or mfa_secret (CWE-200)."""
        return {
            "id": self.id, "tenant": self.tenant, "username": self.username,
            "caps": self.caps, "roles": self.roles, "status": self.status,
            "mfa_enabled": self.mfa_secret is not None,
            "email_verified": bool(self.email_verified),
            "created_at": self.created_at,
        }

    def touch(self) -> None:
        self.updated_at = _now()
