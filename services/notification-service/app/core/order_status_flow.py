"""Flow #3 — ORDER / REQUEST STATUS tracking over WhatsApp. The simplest dynamic Flow: READ-ONLY.

The order (or customer) id is in the Flow seed data; INIT loads the live status from the platform's
system and shows it on one screen; the customer taps Done. No booking, no payment, no session — just
a live read. Pure infra: we never define what a "status" means; the platform's provider returns the
headline + body to show."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.core.booking_flow import SUCCESS, ProviderUnavailable

INIT, BACK, DATA_EXCHANGE = "INIT", "BACK", "data_exchange"
STATUS = "STATUS"


@runtime_checkable
class OrderStatusProvider(Protocol):
    """Implement over your OWN order/request system. `order` is whatever id you seeded the Flow with
    (an order number, a ticket id, a customer id). Raise / non-2xx on failure → retry snackbar."""

    async def status(self, *, tenant: str, order: str) -> dict[str, Any]:
        """-> {"headline": str, "body": str}. headline e.g. "Out for delivery"; body a human summary
        (tracking no., ETA, courier, ...). Both are shown verbatim; format them for the customer."""
        ...


class OrderStatusEngine:
    """FlowHandler (name="order_status"). Register in the FlowRegistry. One STATUS screen (a heading +
    body bound to ${data.headline}/${data.body} and a data_exchange 'Done' footer) → SUCCESS."""

    name = "order_status"

    def __init__(self, *, provider: OrderStatusProvider, strings: dict[str, str] | None = None) -> None:
        self._p = provider
        self._strings = strings or {}

    async def handle(self, payload: dict[str, Any], *, tenant: str = "") -> dict[str, Any]:
        action = payload.get("action")
        token = str(payload.get("flow_token") or "")
        data = payload.get("data") or {}
        if action == DATA_EXCHANGE:
            # the only interaction is the Done footer → complete the flow
            return {"screen": SUCCESS,
                    "data": {"extension_message_response": {"params": {"flow_token": token}}}}
        # INIT (or BACK) → load + show the live status
        order = str(data.get("order") or data.get("customer") or "")
        try:
            st = await self._p.status(tenant=tenant, order=order)
        except ProviderUnavailable:
            return {"screen": STATUS, "data": {
                "headline": self._strings.get("unavailable_title", "Status unavailable"),
                "body": self._strings.get("backend_error", "Sorry, we could not load your order right now."),
                "error_message": self._strings.get("backend_error", "Please try again.")}}
        return {"screen": STATUS, "data": {"headline": st.get("headline", ""),
                                           "body": st.get("body", "")}}
