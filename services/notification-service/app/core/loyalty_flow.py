"""Flow #6 — LOYALTY POINTS REDEMPTION over WhatsApp. A dynamic-Flow handler on the generic ladder,
reusing the booking SELECT/CONFIRM/SUCCESS screens + SessionStore.

Pure infrastructure: we never define what a "point" is, how it is earned, or the reward catalogue —
that is the platform's loyalty program, behind one port. The sender puts the customer id in the Flow
seed data (build_flow(flow_action="data_exchange", data={"customer": "<id>"})); we load THAT
customer's balance + redeemable rewards and let them redeem. Who the customer is (the platform's own
customers, or its customers' customers) is the provider's call — we just pass the id through.

Points are money-like: redeem MUST be atomic + idempotent on the flow_token (no double-redeem)."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.core.booking_flow import (CONFIRM, SELECT, SELECTION_FIELD, SUCCESS, ProviderUnavailable,
                                   SessionStore)

INIT, BACK, DATA_EXCHANGE = "INIT", "BACK", "data_exchange"


@runtime_checkable
class LoyaltyProvider(Protocol):
    """Implement over your OWN loyalty program (or use the wallet-backed adapter). `customer` comes
    from the Flow seed data. Raise / non-2xx on failure → retry snackbar, never an empty catalogue or
    a fake redemption. redeem MUST be atomic + idempotent on `idempotency_key` (the flow_token)."""

    async def balance(self, *, tenant: str, customer: str) -> dict[str, Any]:
        """-> {"points": int, "label"?: str} (label e.g. "1,200 pts"). Shown on the reward screen."""
        ...

    async def rewards(self, *, tenant: str, customer: str) -> list[dict[str, Any]]:
        """The rewards this customer may redeem NOW (you decide affordability). -> [{"id","title",
        "description"?}] (title e.g. "Free coffee — 500 pts"). id comes back at redeem."""
        ...

    async def redeem(self, *, tenant: str, customer: str, reward: str,
                     idempotency_key: str) -> dict[str, Any]:
        """Redeem it. -> {"ok":true,"ref"?} (ref = a voucher/confirmation shown in chat) |
        {"ok":false,"reason"?} (e.g. not enough points / out of stock — shown as a snackbar)."""
        ...


_PICK, _CONFIRM = "pick", "confirm"


class LoyaltyEngine:
    """FlowHandler for a points-redemption Flow. Register in the FlowRegistry alongside
    booking/reschedule. `name` is the FlowRegistry key (the flow name a template routes to) and
    defaults to "loyalty"; a SECOND instance with a distinct name + its own provider is how the same
    generic redeem-a-thing screens back another flow — e.g. name="subscription_renewal", whose provider
    lists the renewable subscriptions as "rewards" and "redeems" one by taking the renewal payment. The
    provider's opaque `customer` id is whatever the sender seeds — a shop's end customer for loyalty, or
    the subscription/tenant being renewed for renewal (the engine passes `tenant` separately already, so
    no protocol change is needed to target the tenant)."""

    def __init__(self, *, provider: LoyaltyProvider, sessions: SessionStore,
                 name: str = "loyalty", strings: dict[str, str] | None = None) -> None:
        self.name = name
        self._p = provider
        self._sessions = sessions
        self._s = strings or {}

    def _label(self, key: str, default: str) -> str:
        return self._s.get(key, default)

    async def handle(self, payload: dict[str, Any], *, tenant: str = "") -> dict[str, Any]:
        token = str(payload.get("flow_token") or "")
        data = payload.get("data") or {}
        action = payload.get("action")
        try:
            return await self._dispatch(tenant, token, action, data)
        except ProviderUnavailable:
            return {"screen": payload.get("screen") or CONFIRM,
                    "data": {"error_message": self._label(
                        "backend_error", "Sorry, we could not load your rewards. Please try again.")}}

    async def _dispatch(self, tenant, token, action, data) -> dict[str, Any]:
        if action == INIT:
            st = {"customer": str(data.get("customer") or ""), "step": _PICK, "reward": ""}
            await self._sessions.save(tenant, token, st)
            return await self._rewards_screen(tenant, st)

        st = await self._sessions.load(tenant, token) or {"customer": "", "step": _PICK}
        step = st.get("step")

        if action == BACK:
            st["step"] = _PICK
            await self._sessions.save(tenant, token, st)
            return await self._rewards_screen(tenant, st)

        if step == _PICK:
            st["reward"] = data.get(SELECTION_FIELD, "")
            if not st["reward"]:
                return await self._rewards_screen(tenant, st,
                                                  error=self._label("required", "Please choose a reward."))
            st["step"] = _CONFIRM
            await self._sessions.save(tenant, token, st)
            return self._confirm_screen()

        if step == _CONFIRM:
            res = await self._p.redeem(tenant=tenant, customer=st.get("customer", ""),
                                       reward=st["reward"], idempotency_key=token)
            if not res.get("ok"):
                return self._confirm_screen(
                    error=res.get("reason") or "Could not redeem that reward. Please try again.")
            await self._sessions.clear(tenant, token)
            return {"screen": SUCCESS, "data": {"extension_message_response": {"params": {
                "flow_token": token, "reward_ref": res.get("ref", "")}}}}

        return self._confirm_screen(error="This session has ended. Please start again.")

    async def _rewards_screen(self, tenant, st, *, error="") -> dict[str, Any]:
        bal = await self._p.balance(tenant=tenant, customer=st.get("customer", ""))
        rewards = await self._p.rewards(tenant=tenant, customer=st.get("customer", ""))
        title = self._label("pick_reward", "Redeem your points")
        bal_label = bal.get("label") or (f"{bal.get('points', 0)} pts")
        return {"screen": SELECT, "data": {"dimension": "reward",
                "title": f"{title} — {bal_label}", "options": rewards,
                **({"error_message": error} if error else {})}}

    def _confirm_screen(self, *, error="") -> dict[str, Any]:
        return {"screen": CONFIRM, "data": {"title": self._label("confirm", "Confirm redemption"),
                **({"error_message": error} if error else {})}}
