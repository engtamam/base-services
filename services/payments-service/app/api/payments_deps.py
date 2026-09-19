"""Builders that assemble the payments domain from the container + settings, per request.

Kept in one place so the internal and public routers construct the SAME wiring (one merchant secret
store, one request store, one service), and so the sealed-credential store is created identically
everywhere — never accidentally with the wrong collection/salt.
"""
from __future__ import annotations

from fastapi import Request

from app.config import Settings, get_settings
from app.core.merchant_credentials import MerchantGateways, merchant_secret_store
from app.core.option_gate import OptionGate
from app.core.payment_requests import PaymentRequestStore
from app.core.payments import PaymentService
from app.core.secret_store import SecretStore


def _allowed_hosts(settings: Settings) -> frozenset[str]:
    raw = (settings.PAYMENTS_REDIRECT_ALLOWED_HOSTS or "").strip()
    if not raw:
        return frozenset()
    if raw.startswith("["):
        import json
        try:
            return frozenset(str(h).strip().lower() for h in json.loads(raw) if str(h).strip())
        except (ValueError, TypeError):
            return frozenset()
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def build_merchant_secrets(request: Request, settings: Settings | None = None) -> SecretStore:
    settings = settings or get_settings()
    return merchant_secret_store(
        request.app.state.container.datastore, settings.SECRETS_KEK,
        rotation_window_s=settings.SECRETS_ROTATION_WINDOW_SECONDS)


def build_gateways(request: Request, settings: Settings | None = None) -> MerchantGateways:
    settings = settings or get_settings()
    return MerchantGateways(
        build_merchant_secrets(request, settings),
        console_link_base=settings.PAYMENTS_CONSOLE_LINK_BASE,
        gateway_timeout=settings.PAYMENTS_GATEWAY_TIMEOUT_SECONDS)


def build_option_gate(request: Request) -> OptionGate:
    """The per-tenant per-option enable/lock store (operator-owned). One builder so the enforcement
    path (create_link) and the operator surface read exactly the same gate."""
    return OptionGate(request.app.state.container.datastore)


def build_payment_service(request: Request, settings: Settings | None = None) -> PaymentService:
    settings = settings or get_settings()
    container = request.app.state.container
    gate = build_option_gate(request)
    return PaymentService(
        requests=PaymentRequestStore(container.datastore),
        gateways=build_gateways(request, settings),
        event_bus=container.event_bus,
        redirect_base_url=settings.PAYMENTS_REDIRECT_BASE_URL,
        allowed_hosts=_allowed_hosts(settings),
        link_ttl_seconds=settings.PAYMENTS_LINK_TTL_SECONDS,
        # The external gateway pay-link is this service's lockable option. An operator can disable or
        # hard-lock it per shop (or fleet-wide); create_link refuses when it is off.
        is_enabled=lambda tenant: gate.is_enabled(tenant, "external_paylink"))
