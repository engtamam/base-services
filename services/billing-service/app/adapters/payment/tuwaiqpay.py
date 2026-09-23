"""TuwaiqPay (KSA) — verified against the provider's own Integration Docs v2.0
(docs/integrations/tuwaiqpay-docs.source.html, captured 2026-09-06).

A HOSTED-LINK gateway, not a token gateway:
  auth     POST /auth/authenticate  {username, userNameType, password} -> data.access_token (Bearer,
           ~3600s). username is a mobile (+9665…) or email; cached until just before expiry.
  bill     POST /integration/bills  -> data.link (the hosted payment page) + merchantTransactionId
           (our correlation key) + transactionId (status key). amount is a FLOAT in major units (SAR),
           currencyId 1=SAR/2=USD/3=EUR. This is prepare_checkout — there is NO card-on-file charge.
  status   GET  /integration/bills/transaction/{transactionId} -> transactionStatus. This is
           fetch_status, and it is also how a webhook is VERIFIED (see verify_webhook).

DELIBERATE FAIL-CLOSED GAPS (the provider's API simply does not offer these — flagged to them):
  * charge(token): TuwaiqPay saves no card, so there is no synchronous token charge. Raises
    GatewayUnavailable rather than pretend — a subscription renewal on this gateway is a fresh
    payment LINK per cycle (delivered out of band, e.g. WhatsApp), never a silent auto-charge.
  * refund(): no refund endpoint exists in the API. Raises GatewayUnavailable, never a fake success.
  * webhook signature: the webhook is UNSIGNED. So we do not trust the POST body — verify_webhook
    re-queries the transaction from TuwaiqPay (server-to-server, authenticated) and trusts THAT.
    An attacker cannot forge a status our own authenticated GET does not return.

Credential mapping (reuses the shared PAYMENT_* slot): PAYMENT_API_KEY = username (+9665…/email),
PAYMENT_EXTRA_SECRET = password. PAYMENT_API_BASE overrides the base URL (UAT vs Production).
"""
from __future__ import annotations

import time

from app.adapters.billing_http import bearer, get_json, post_json
from app.adapters.payment._common import major, to_minor
from app.ports.payment_gateway import Charge, CheckoutSession, GatewayUnavailable, WebhookEvent

# Provider status -> our normalised status. SETTLED/APPROVED/PENDING_SETTLEMENT are all "money moved".
_PAID = {"SETTLED", "APPROVED", "PENDING_SETTLEMENT"}
_FAILED = {"FAILED", "DECLINED"}
_CANCELLED = {"CANCELLED"}
_CURRENCY_ID = {"SAR": 1, "USD": 2, "EUR": 3}
_DEFAULT_METHODS = ["VISA", "MASTER", "MADA", "AMEX", "STC_PAY", "APPLE_PAY"]
_DEFAULT_BASE = "https://onboarding-prod.tuwaiqpay.com.sa/api/v1"


def _norm(status: str) -> str:
    s = (status or "").upper()
    return ("succeeded" if s in _PAID else "failed" if s in _FAILED
            else "cancelled" if s in _CANCELLED else "pending")


class TuwaiqPayGateway:
    driver = "tuwaiqpay"
    # No saved-card/token charge exists on TuwaiqPay (charge() below always raises). The billing core
    # reads this: a renewal on such a gateway is collected with a hosted PAY LINK (minted + emitted for
    # a channel to send), not a doomed card retry. Every other adapter defaults to True (getattr).
    supports_token_charge = False

    def __init__(self, api_key: str = "", base: str = "", extra_secret: str = "",
                 return_url: str = "", webhook_tolerance: int = 300, **_) -> None:
        # api_key = username (+9665… or email), extra_secret = password.
        if not (api_key and extra_secret):
            raise GatewayUnavailable(
                "PAYMENT_API_KEY (TuwaiqPay username/mobile) + PAYMENT_EXTRA_SECRET (password) required")
        self._user = api_key
        self._password = extra_secret
        self._base = (base or _DEFAULT_BASE).rstrip("/")
        self._return_url = return_url
        self._token = ""
        self._token_exp = 0.0                       # monotonic seconds

    # ── auth (cached) ────────────────────────────────────────────────────────────
    def _access_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_exp:
            return self._token
        name_type = "MOBILE" if self._user.strip().startswith("+") else "EMAIL"
        code, resp = post_json(f"{self._base}/auth/authenticate",
                               {"username": self._user, "userNameType": name_type,
                                "password": self._password},
                               {"Accept": "application/json", "X-Language": "en"})
        if code == 0:
            raise GatewayUnavailable("TuwaiqPay unreachable (auth)")
        token = str((resp.get("data") or {}).get("access_token", ""))
        if not token:
            raise GatewayUnavailable(f"TuwaiqPay auth failed: {resp.get('message', code)}")
        # expires_in is seconds; refresh 100s early so an in-flight call never uses an expiring token.
        ttl = int((resp.get("data") or {}).get("expires_in", 3600))
        self._token, self._token_exp = token, now + max(60, ttl - 100)
        return token

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": bearer(self._access_token()), "Accept": "application/json",
                "X-Language": "en"}

    # ── prepare_checkout — the ONLY way to take money on TuwaiqPay ────────────────
    def prepare_checkout(self, *, amount_minor: int, currency: str, reference: str,
                         return_url: str = "", metadata: dict | None = None) -> CheckoutSession:
        """Create a bill and return its hosted payment link. Customer name + mobile are REQUIRED by
        TuwaiqPay; they are read from metadata (customerName / customerMobilePhone) — a caller that
        omits them gets a clear refusal, not a Meta-style opaque 400."""
        meta = metadata or {}
        name = str(meta.get("customerName") or meta.get("customer_name") or "")
        phone = str(meta.get("customerMobilePhone") or meta.get("customer_mobile") or "")
        if not (name and phone):
            return CheckoutSession(ok=False, provider=self.driver,
                                   error="TuwaiqPay requires customerName + customerMobilePhone in metadata")
        cur = (currency or "SAR").upper()
        payload = {
            "amount": float(major(amount_minor, cur)),          # minor -> major at the currency's real exponent (KWD/BHD 3-dp)
            "actionDateInDays": int(meta.get("actionDateInDays", 3)),
            "currencyId": _CURRENCY_ID.get(cur, 1),
            "supportedPaymentMethods": list(meta.get("supportedPaymentMethods") or _DEFAULT_METHODS),
            "description": str(meta.get("description") or reference or "Payment"),
            "customerName": name,
            "customerMobilePhone": phone,
            "includeVat": bool(meta.get("includeVat", False)),
        }
        email = str(meta.get("customerEmail") or "")
        if email:
            payload["customerEmail"] = email
        try:
            code, resp = post_json(f"{self._base}/integration/bills", payload, self._auth_headers())
        except GatewayUnavailable as ex:
            return CheckoutSession(ok=False, provider=self.driver, error=str(ex))
        data = resp.get("data") or {}
        link = str(data.get("link", ""))
        if code == 0 or not link:
            return CheckoutSession(ok=False, provider=self.driver,
                                   error=f"TuwaiqPay bill failed: {resp.get('message', code)}")
        # id = transactionId: the key fetch_status + the webhook resolve on. merchantTransactionId is
        # surfaced in the widget for the caller to persist alongside its own reference.
        return CheckoutSession(
            ok=True, provider=self.driver, id=str(data.get("transactionId", "")),
            redirect_url=link, expires_at=str(data.get("expireDate", "")),
            widget={"merchantTransactionId": str(data.get("merchantTransactionId", "")),
                    "billId": str(data.get("billId", "")), "qrCode": str(data.get("qrCode", ""))})

    def fetch_status(self, *, provider_id: str, currency: str = "") -> Charge:
        """GET the transaction's current status — reconciliation, and the trust anchor for webhooks."""
        code, resp = get_json(f"{self._base}/integration/bills/transaction/{provider_id}",
                              self._auth_headers())
        if code == 0:
            raise GatewayUnavailable("TuwaiqPay unreachable (status)")
        data = resp.get("data") or {}
        status = _norm(str(data.get("transactionStatus", "")))
        bill = data.get("bill") or {}
        return Charge(ok=status == "succeeded", status=status, provider=self.driver,
                      provider_id=str(data.get("transactionId", provider_id)),
                      amount_minor=to_minor(bill.get("amount", 0) or 0,
                                            str(bill.get("currency", currency) or currency)),
                      currency=str(bill.get("currency", currency) or currency))

    def verify_webhook(self, *, headers: dict[str, str], body: bytes, secret: str) -> WebhookEvent:
        """The webhook is UNSIGNED, so we do NOT trust the POST body's status. We take only the
        transactionId from it and RE-QUERY TuwaiqPay (authenticated, server-to-server); the fetched
        status is the truth. verified=True only when our own GET confirms a real outcome — an attacker
        cannot forge a status TuwaiqPay's API does not itself report."""
        import json
        try:
            payload = json.loads(body.decode("utf-8", "replace")) if body else {}
        except Exception:  # noqa: BLE001
            return WebhookEvent(verified=False, type="unknown")
        details = payload.get("transactionDetails") or {}
        tx_id = str(details.get("transactionId", ""))
        if not tx_id:
            return WebhookEvent(verified=False, type="unknown")
        try:
            confirmed = self.fetch_status(provider_id=tx_id)     # verify-by-fetch
        except GatewayUnavailable:
            return WebhookEvent(verified=False, type="unknown", provider_id=tx_id)
        type_map = {"succeeded": "payment.succeeded", "failed": "payment.failed",
                    "cancelled": "payment.failed"}
        etype = type_map.get(confirmed.status, "unknown")
        # verified only for a settled outcome we confirmed; a still-pending webhook is not actionable.
        return WebhookEvent(verified=confirmed.status in ("succeeded", "failed", "cancelled"),
                            type=etype, provider_id=confirmed.provider_id, raw={"status": confirmed.status})

    # ── deliberately unsupported (fail-closed, never faked) ──────────────────────
    def charge(self, *, amount_minor: int, currency: str, token: str, idempotency_key: str,
               description: str = "", metadata: dict | None = None) -> Charge:
        raise GatewayUnavailable(
            "TuwaiqPay has no saved-card/token charge — use prepare_checkout (hosted link); a renewal "
            "on TuwaiqPay is a fresh payment link per cycle")

    def refund(self, *, provider_id: str, amount_minor: int | None = None,
               currency: str = "SAR") -> Charge:
        raise GatewayUnavailable("TuwaiqPay exposes no refund API endpoint")
