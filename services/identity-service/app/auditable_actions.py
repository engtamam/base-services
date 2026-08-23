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
    # NOTE: `tenant.created` is NOT declared here — this service does not create tenants
    # (tenant-service does, and records it). A service must not declare a control it never performs;
    # doing so makes a compliance map read "witnessed" over an action this service never takes.

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
    {
        "action": "member.roles_changed",
        "source": "admin",
        "critical": True,
        "resource": "member",
        "why": "changing a member's roles changes what power they hold in the tenant; an "
               "unattributable elevation is indistinguishable from an attacker's, exactly like a grant",
    },
    {
        "action": "member.status_changed",
        "source": "admin",
        "critical": True,
        "resource": "member",
        "why": "suspending a member revokes their access to the organisation and reactivating "
               "restores it; either is disputable exactly like a role change, and an unattributable "
               "suspension is indistinguishable from an attacker locking a rival out",
    },
    {
        "action": "auth.self_deleted",
        "source": "user",
        "critical": True,
        "resource": "user",
        "why": "a person erasing themselves revokes their own access and may delete the Person "
               "entirely; they can later dispute it, and a self-service deletion with no record is "
               "indistinguishable from an account takeover that removed the real owner",
    },
    {
        "action": "tenant.owner_seeded",
        "source": "internal",
        "critical": True,
        "resource": "tenant",
        "why": "seeding a tenant's FIRST owner is the most consequential grant this service makes — "
               "it hands the keys to a whole organisation to a human, over a service-to-service "
               "call, with nobody already inside to witness it. An owner nobody can account for is "
               "exactly what an attacker wants, so the grant is refused if it cannot be recorded",
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
        "action": "identity.rotate_signing_key",
        "source": "admin",
        "critical": True,
        "resource": "secret",
        "why": "rotating the key that signs every token the whole platform trusts is the highest-"
               "leverage credential change there is. An unrecorded rotation is indistinguishable "
               "from an attacker swapping in a signing key of their own, so if we cannot witness it "
               "we must not perform it. Cron-driven, but each actual rotation is one critical event",
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
    # ── machine identity (service accounts / OAuth2 client-credentials) ────────
    # Issuing, rotating, and revoking a service account are changes to WHO CAN GET IN — a machine
    # credential that can act on the API — so they follow the same line as member.* and operator.*:
    # critical and write-ahead. The token exchange itself is the machine's login: on the volume
    # path, recorded but never blocking, exactly like a human login.
    {
        "action": "service_account.issued",
        "source": "admin",
        "critical": True,
        "resource": "service_account",
        "why": "minting a machine credential grants standing power to act on the API without a "
               "person behind it; an unrecorded issue is an unattributable actor in every log it "
               "then appears in, and there is no other place that fact is captured",
    },
    {
        "action": "service_account.rotated",
        "source": "admin",
        "critical": True,
        "resource": "service_account",
        "why": "rotation overwrites the old secret the moment it happens, so an unrecorded rotation "
               "cannot be reconstructed — the same reasoning as an operator secret set",
    },
    {
        "action": "service_account.revoked",
        "source": "admin",
        "critical": True,
        "resource": "service_account",
        "why": "revocation is how an integration is cut off; whether and when it happened is exactly "
               "what an incident review asks, and it must be provable",
    },
    {
        "action": "service_account.token_issued",
        "source": "user",
        "critical": False,
        "resource": "service_account",
        "why": "a client-credentials exchange is the machine's login — the volume path. Recorded so "
               "an integration's activity is attributable, but never blocking: refusing to mint a "
               "token because the log is down would be an outage that protects nobody",
    },
    {
        "action": "service_account.auth_failed",
        "source": "user",
        "critical": False,
        "resource": "service_account",
        "why": "a burst of failed client-credentials exchanges is the brute-force signal for machine "
               "identities, exactly as failed logins are for humans (ISO 27001 A.8.15, NIST AU-2, "
               "PCI-DSS 10.2); declared, but not critical for the same denial-of-service reason",
    },
]
