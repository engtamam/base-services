# =============================================================================
# api-gateway — no own /api/v1 business routes; the proxy (app/api/proxy.py)
# forwards everything to upstreams. This router stays empty on purpose.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
