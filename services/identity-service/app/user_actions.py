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
]
