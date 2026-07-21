"""identity-service — auditable actions.

This service is where the platform's most attackable moments happen: credentials change, MFA is
enrolled, and people are given or denied access to a tenant. It is also the service that owns the
pseudonym→person LINKAGE, so it is the only place an `actor` can be resolved back to a human — and
the only place an erasure can truly sever that.

Criticality here follows one line: **anything that changes who can get in, or who has power, is
critical.** Account takeover looks exactly like a password change followed by an MFA enrolment, so
if we cannot record those we should not perform them. Everything on the volume path — logins,
refreshes, logouts — fails open and is counted: refusing every login because the audit log is
having a bad minute would be an outage that protects nobody.

Failed logins are declared but NOT critical, and the reasoning is worth stating because it looks
backwards. A burst of failures is the most common first sign of an attack, so the record matters —
but making it critical would mean an audit outage stops people signing in at all, and an attacker
who could knock over the log would gain a free denial-of-service against the whole platform.
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "auth.email_verified",
        "source": "user",
        "critical": False,
    },
    # ── auth: the volume path. Recorded, never blocking. ─────────────────────
    {
        "action": "auth.registered",
        "source": "auth",
        "critical": False,
        "resource": "user",
    },
    {
        "action": "auth.login_succeeded",
        "source": "auth",
        "critical": False,
        "resource": "user",
    },
    {
        "action": "auth.login_failed",
        "source": "auth",
        "critical": False,
        "resource": "user",
    },
    {
        "action": "auth.token_refreshed",
        "source": "auth",
        "critical": False,
        "resource": "session",
    },
    {
        "action": "auth.logged_out",
        "source": "auth",
        "critical": False,
        "resource": "session",
    },
    {
        "action": "auth.mfa_verified",
        "source": "auth",
        "critical": False,
        "resource": "user",
    },
    {
        "action": "person.claimed",
        "source": "user",
        "critical": False,
        "resource": "person",
    },
    {
        "action": "person.login_succeeded",
        "source": "auth",
        "critical": False,
        "resource": "person",
    },
    {
        "action": "tenant.created",
        "source": "user",
        "critical": False,
        "resource": "tenant",
    },

    # ── credentials: account takeover looks exactly like this ────────────────
    {
        "action": "auth.password_changed",
        "source": "auth",
        "critical": True,
        "resource": "user",
        "why": "a password change is the last step of most account takeovers; without a record "
               "there is no way to tell the owner's change from an attacker's",
    },
    {
        "action": "auth.mfa_enrolled",
        "source": "auth",
        "critical": True,
        "resource": "user",
        "why": "enrolling a new MFA factor hands someone permanent access; an unattributable "
               "enrolment is indistinguishable from an attacker locking the owner out",
    },

    # ── access: who is allowed inside this tenant ────────────────────────────
    {
        "action": "member.added",
        "source": "admin",
        "critical": True,
        "resource": "member",
        "why": "adding a member grants access to another organisation's data; an unattributable "
               "grant is indistinguishable from an attacker's",
    },
    {
        "action": "member.removed",
        "source": "admin",
        "critical": True,
        "resource": "member",
        "why": "someone lost access and can dispute it; without a record we cannot say who "
               "removed them or when",
    },

    # ── internal: service-to-service, in no panel and no catalog ─────────────
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "resource": "subject",
        "why": "erasing the identity linkage is irreversible and orphans every pseudonym in the "
               "platform; the data is gone, so nothing but this record can show we honoured the "
               "DSAR — or prove we erased the right person",
    },
    {
        "action": "ratelimit.unblocked",
        "source": "internal",
        "critical": True,
        "resource": "ratelimit",
        "why": "unblocking lifts a security control; turning off a defence must never be quiet, "
               "and repeated unblocks for one actor are themselves a signal",
    },
    {
        "action": "operator.secret_set",
        "source": "admin",
        "critical": True,
        "resource": "secret",
        "why": "a credential change is the action that most needs attribution: the previous value "
               "is gone the moment it happens, so an unrecorded rotation cannot be reconstructed, "
               "and an unattributable one is indistinguishable from an attacker's",
    },
    {
        "action": "operator.secret_cleared",
        "source": "admin",
        "critical": True,
        "resource": "secret",
        "why": "clearing a credential silently reverts the service to whatever the environment "
               "holds, which is a behaviour change nobody would otherwise see",
    },
    {
        "action": "operator.config_changed",
        "source": "admin",
        "critical": False,
        "resource": "config",
        "why": "policy changes explain later behaviour — why a session expired sooner, why an "
               "invite stopped working — and that history is unavailable anywhere else. Not "
               "critical: refusing to change a TTL because the log is down protects nothing",
    },
]
