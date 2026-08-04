# =============================================================================
# ticket-service — User tier (/api/v1/*, token-authenticated)
# -----------------------------------------------------------------------------
# The real business routes live in app/api/v1/tickets.py. Every route there is
# protected: require_user verifies the Bearer token (deny-by-default),
# require_any_capability adds RBAC (tickets.self / tickets.manage), rate_limit
# throttles, tenant comes from the token, and the workflow is enforced in core.
# =============================================================================
from __future__ import annotations

from app.api.v1.tickets import router

__all__ = ["router"]
