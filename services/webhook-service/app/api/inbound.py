# =============================================================================
# webhook-service — INBOUND ingest. The signature IS the authentication.
# -----------------------------------------------------------------------------
# Hot path: verify → dedupe → one write → publish → 200. No synchronous work.
# We read the RAW body (never a re-serialization) because that is what was signed.
# =============================================================================
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.api.v1.webhooks import build_inbound
from app.config import get_settings

router = APIRouter(tags=["inbound"])

# Where a provider puts its event id. Dedupe uses the PROVIDER's id, never a body hash.
_ID_FIELDS = ("id", "event_id", "eventId", "delivery_id")


def _provider_event_id(payload: dict, header_id: str | None) -> str:
    if header_id:
        return header_id
    for field in _ID_FIELDS:
        value = payload.get(field)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return ""


@router.post("/inbound/{source}")
async def ingest(source: str, request: Request,
                 x_webhook_signature: str = Header(default="", alias="X-Webhook-Signature"),
                 x_webhook_id: str = Header(default="", alias="X-Webhook-Id")) -> dict:
    settings = get_settings()
    body = await request.body()                       # the exact bytes that were signed
    if len(body) > settings.MAX_PAYLOAD_BYTES:
        raise HTTPException(413, "PAYLOAD_TOO_LARGE")
    try:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            payload = {"_": payload}
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "INVALID_JSON")

    from app.core.inbound_service import InboundRejected

    try:
        return await (await build_inbound(request)).ingest(
            source=source, body=body, signature=x_webhook_signature,
            provider_event_id=_provider_event_id(payload, x_webhook_id),
            payload=payload, now=int(time.time()))
    except InboundRejected as exc:
        # 400, never 5xx — a bad signature is the sender's problem, and a 5xx would make
        # the provider retry a payload we will never accept.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
