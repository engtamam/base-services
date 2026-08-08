"""identity-service — end-user (self-service) actions: a person managing their OWN account.

These are the AUTHENTICATED self routes (Depends(require_user)): view my profile, change my own
password, enrol/verify my MFA. They act only on the caller's own identity (taken from the token),
so `identity.self` is the capability that represents "my own account" — advertised in the manifest,
granted to every registered user by the platform's authz. The pre-auth flow (register / login /
refresh) is deliberately ABSENT: a logged-in user does not register or log in, and the app catalog is
reached only WITH a token. A tenant admin managing OTHER members (members:*) is a separate, more
privileged surface (identity's admin_actions), not self-service. Every action re-checks auth and acts
on the caller's own record; this file only decides what the app shows.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    {"id": "identity.me", "label": "View my profile", "method": "GET",
     "path": "/api/v1/auth/me", "capability": "identity.self", "group": "My account"},
    {"id": "identity.change_password", "label": "Change my password", "method": "POST",
     "path": "/api/v1/auth/change-password", "capability": "identity.self", "group": "My account",
     "confirm": True,
     "params": [{"name": "old_password", "in": "body", "type": "string", "required": True},
                {"name": "new_password", "in": "body", "type": "string", "required": True}]},
    {"id": "identity.mfa_enroll", "label": "Set up MFA", "method": "POST",
     "path": "/api/v1/auth/mfa/enroll", "capability": "identity.self", "group": "My account"},
    {"id": "identity.mfa_verify", "label": "Verify MFA", "method": "POST",
     "path": "/api/v1/auth/mfa/verify", "capability": "identity.self", "group": "My account",
     "params": [{"name": "code", "in": "body", "type": "string", "required": True}]},
    {"id": "identity.delete_me", "label": "Delete my account", "method": "DELETE",
     "path": "/api/v1/auth/me", "capability": "identity.self", "group": "My account",
     "confirm": True, "danger": True},
]

# Routes deliberately NOT rendered as controls. Machine-checked by safety:action-coverage: every
# user-tier route must be declared above or excluded here WITH a reason.
UNDECLARED_ROUTES: list[dict[str, str]] = [
    {"method": "POST", "path": "/api/v1/auth/person/claim",
     "why": "pre-auth: redeeming an invite token is done by someone who has no session yet, which is the whole point of an invite"},
    {"method": "POST", "path": "/api/v1/auth/person/login",
     "why": "pre-auth: you cannot be shown a log-in control in an app you are already in"},
    {"method": "POST", "path": "/api/v1/auth/tenant",
     "why": "signup-time tenant creation, reached before the caller belongs to a tenant and therefore before any catalog is fetched"},
    {"method": "POST", "path": "/api/v1/auth/register",
     "why": "pre-auth: the app catalog is fetched WITH a token, so its caller has already registered"},
    {"method": "POST", "path": "/api/v1/auth/login",
     "why": "pre-auth: you cannot be shown a 'log in' control in an app you are already logged into"},
    {"method": "POST", "path": "/api/v1/auth/verify-email",
     "why": "pre-auth token redemption: reached by clicking a link in an email, by someone who "
            "has no session by definition — the catalog is fetched WITH a token, and an "
            "unverified account cannot obtain one"},
    {"method": "POST", "path": "/api/v1/auth/resend-verification",
     "why": "pre-auth: the caller is precisely the person who cannot log in yet, so it belongs to "
            "the signup screen rather than to a catalog rendered for an authenticated user"},
    {"method": "POST", "path": "/api/v1/auth/refresh",
     "why": "session plumbing the client driver handles automatically, not a button"},
    {"method": "POST", "path": "/api/v1/auth/logout",
     "why": "session plumbing owned by the app shell's own sign-out, not a per-service control"},
    {"method": "POST", "path": "/api/v1/oauth/token",
     "why": "the OAuth2 client-credentials token exchange — a machine endpoint called by a partner's "
            "own code with a client_id/secret, not a human panel control. Managing the service "
            "accounts that use it IS a control (see admin_actions: issue/rotate/revoke)"},
]
