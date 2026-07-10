# =============================================================================
# notification-service — API dependencies
# -----------------------------------------------------------------------------
# require_user now performs REAL token verification (app/api/auth.py) via the
# configured TokenVerifier port; the default 'null' verifier still denies all
# (deny-by-default). Re-exported here so routes keep importing from one place.
# =============================================================================
from __future__ import annotations

from fastapi import Request

from app.api.auth import Principal, require_user
from app.container import Container

__all__ = ["get_container", "get_guard", "require_user", "Principal"]


def get_container(request: Request) -> Container:
    """Expose the composition root's wired ports to routes."""
    return request.app.state.container


def get_guard(request: Request):
    """Expose the CWE self-guard to routes."""
    return request.app.state.cwe_guard
