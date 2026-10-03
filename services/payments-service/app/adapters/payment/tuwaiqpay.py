"""TuwaiqPay (KSA) gateway — verified against the provider's Integration Docs v2.0
(docs/integrations/tuwaiqpay-api-reference.md, captured 2026-09-06).

A HOSTED-LINK gateway, not a token gateway — exactly the conduit shape:
  auth    POST /auth/authenticate {username,userNameType,password} -> data.access_token (Bearer,
          ~3600s, cached). username is a mobile (+9665…) or email.
  bill    POST /integration/bills -> data.link (hosted page) + transactionId (status key) +
          merchantTransactionId (our correlation key). amount is a FLOAT in major units; currencyId
          1=SAR/2=USD/3=EUR. THIS is prepare_checkout — there is no saved-card charge.
  status  GET  /integration/bills/transaction/{transactionId} -> transactionStatus. This is
          fetch_status, and the trust anchor for the (unsigned) webhook.

The webhook is UNSIGNED (the docs describe no HMAC), so verify_webhook takes ONLY the transactionId
from the POST and RE-QUERIES TuwaiqPay server-to-server; the fetched status is the truth. An attacker
cannot forge a status our own authenticated GET does not itself report.

Per-tenant credentials (sealed MERCHANT_*): api_key = username (+9665…/email), extra_secret =
password, base overrides the environment (UAT vs Production).
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

from app.adapters.payment_http import bearer, get_json, post_json
from app.core.currency import to_major, to_minor
from app.ports.payment_gateway import (CANCELLED, FAILED, PENDING, SUCCEEDED, CheckoutSession,
                                       GatewayUnavailable, PaymentOutcome)

# L5: PENDING_SETTLEMENT is deliberately NOT paid. It means the gateway accepted the payment but has
# not yet SETTLED/APPROVED it (capture pending); mapping it to SUCCEEDED would complete — and emit the
# money-moved event — before capture, so a later reversal would leave us over-committed. We complete
# only on a confirmed capture (SETTLED/APPROVED); PENDING_SETTLEMENT falls through to PENDING and a
# later webhook/reconcile settles it. Revisit only if TuwaiqPay confirms PENDING_SETTLEMENT is a
# guaranteed capture.
_PAID = {"SETTLED", "APPROVED"}
_FAILED = {"FAILED", "DECLINED"}
_CANCELLED = {"CANCELLED"}
_CURRENCY_ID = {"SAR": 1, "USD": 2, "EUR": 3}
_ID_CURRENCY = {v: k for k, v in _CURRENCY_ID.items()}   # 1 -> SAR … for reverse-mapping status
_DEFAULT_METHODS = ["VISA", "MASTER", "MADA", "AMEX", "STC_PAY", "APPLE_PAY"]
_DEFAULT_BASE = "https://onboarding-prod.tuwaiqpay.com.sa/api/v1"
# L4: transactionId is interpolated into the status URL path — pin it to an id-shaped charset/length
# so a "../"-style value can never traverse or inject into the path (quote() is a second guard).
_PROVIDER_REF_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}\Z")


def _norm(status: str) -> str:
    s = (status or "").upper()
    return (SUCCEEDED if s in _PAID else FAILED if s in _FAILED
            else CANCELLED if s in _CANCELLED else PENDING)


def _iso_currency(raw, fallback: str) -> str:
    """Normalise TuwaiqPay's status/webhook currency to an ISO code so it compares against our stored
    ISO currency. UNCONFIRMED against live UAT (2026-09-19): the create response echoes "SAR", the
    webhook nests {"currency":{"nameEn":"SAR"}}, and the numeric currencyId ("1") is plausible — a raw
    "1" would flag every real SAR payment as a mismatch. Handle all three shapes; capture a real UAT
    status fixture to lock this down."""
    if isinstance(raw, dict):
        raw = raw.get("nameEn") or raw.get("code") or raw.get("name") or ""
    s = str(raw or "").strip().upper()
    if not s:
        return fallback
    return _ID_CURRENCY.get(int(s), fallback) if s.isdigit() else s


class TuwaiqPayGateway:
    driver = "tuwaiqpay"

    def __init__(self, *, api_key: str = "", extra_secret: str = "", base: str = "",
                 timeout: int = 30, **_) -> None:
        if not (api_key and extra_secret):
            raise GatewayUnavailable(
                "TuwaiqPay needs MERCHANT_API_KEY (username/mobile) + MERCHANT_EXTRA_SECRET (password)")
        self._user = api_key
        self._password = extra_secret
        self._base = (base or _DEFAULT_BASE).rstrip("/")
        self._timeout = timeout
        self._token = ""
        self._token_exp = 0.0

    def _access_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_exp:
            return self._token
        name_type = "MOBILE" if self._user.strip().startswith("+") else "EMAIL"
        code, resp = post_json(f"{self._base}/auth/authenticate",
                               {"username": self._user, "userNameType": name_type,
                                "password": self._password},
                               {"X-Language": "en"}, timeout=self._timeout)
        if code == 0:
            raise GatewayUnavailable("TuwaiqPay unreachable (auth)")
        token = str((resp.get("data") or {}).get("access_token", ""))
        if not token:
            raise GatewayUnavailable(f"TuwaiqPay auth failed: {resp.get('message', code)}")
        ttl = int((resp.get("data") or {}).get("expires_in", 3600))
        self._token, self._token_exp = token, now + max(60, ttl - 100)
        return token

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": bearer(self._access_token()), "X-Language": "en"}

    def prepare_checkout(self, *, amount_minor: int, currency: str, reference: str,
                         return_url: str = "", metadata: dict | None = None) -> CheckoutSession:
        meta = metadata or {}
        name = str(meta.get("customerName") or meta.get("customer_name") or "")
        phone = str(meta.get("customerMobilePhone") or meta.get("customer_mobile") or "")
        if not (name and phone):
            return CheckoutSession(
                ok=False, provider=self.driver,
                error="TuwaiqPay requires customerName + customerMobilePhone in metadata")
        cur = (currency or "SAR").upper()
        # I3: TuwaiqPay's bill payload (docs/integrations/tuwaiqpay-api-reference.md) has NO return/
        # callback URL field — the customer is not bounced back to the shop by the gateway — so our
        # return_url is intentionally not forwarded here (the shop reconciles via webhook/status).
        payload = {
            # TuwaiqPay's `amount` is a JSON number in major units (docs: `150.00`), so it must be a
            # numeric literal, not a string. `to_major` gives the EXACT currency-quantized Decimal and
            # float() only crosses the JSON boundary: for a value quantized to the currency's 2–3
            # decimals, Python's shortest round-trip repr serialises the exact decimal (150.00 → 150.0,
            # 8.61 → 8.61), never a binary tail — the amount on the wire equals the integer minor units.
            "amount": float(to_major(int(amount_minor), cur)),
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
            code, resp = post_json(f"{self._base}/integration/bills", payload,
                                   self._auth_headers(), timeout=self._timeout)
        except GatewayUnavailable as ex:
            return CheckoutSession(ok=False, provider=self.driver, error=str(ex))
        data = resp.get("data") or {}
        link = str(data.get("link", ""))
        if code == 0 or not link:
            return CheckoutSession(ok=False, provider=self.driver,
                                   error=f"TuwaiqPay bill failed: {resp.get('message', code)}")
        return CheckoutSession(
            ok=True, provider=self.driver, provider_ref=str(data.get("transactionId", "")),
            redirect_url=link, expires_at=str(data.get("expireDate", "")),
            extra={"merchantTransactionId": str(data.get("merchantTransactionId", "")),
                   "billId": str(data.get("billId", "")), "qrCode": str(data.get("qrCode", ""))})

    def fetch_status(self, *, provider_ref: str, currency: str = "") -> PaymentOutcome:
        if not _PROVIDER_REF_RE.match(provider_ref or ""):
            raise GatewayUnavailable("TuwaiqPay: malformed transactionId")
        ref = quote(provider_ref, safe="")   # L4: never interpolate a raw id into the URL path
        code, resp = get_json(f"{self._base}/integration/bills/transaction/{ref}",
                              self._auth_headers(), timeout=self._timeout)
        if code == 0:
            raise GatewayUnavailable("TuwaiqPay unreachable (status)")
        data = resp.get("data") or {}
        status = _norm(str(data.get("transactionStatus", "")))
        bill = data.get("bill") or {}
        cur = _iso_currency(bill.get("currency"), currency)
        amount_minor = to_minor(bill.get("amount", 0) or 0, cur)
        # payments-009: a SUCCEEDED status whose amount we could not parse (missing/zero in the response
        # shape) must NOT be returned as a confirmed 0 — apply_outcome would then refuse the REAL success
        # as an amount mismatch and the payment would look failed. Treat it as a transient gateway
        # problem (retry via reconcile) until the UAT status shape is confirmed, rather than a false 0.
        if status == SUCCEEDED and amount_minor <= 0:
            raise GatewayUnavailable("TuwaiqPay: success with unparseable amount (retry)")
        return PaymentOutcome(
            status=status, provider=self.driver,
            provider_ref=str(data.get("transactionId", provider_ref)),
            amount_minor=amount_minor,
            currency=cur, verified=True, raw={"status": status})

    def verify_webhook(self, *, headers: dict[str, str], body: bytes,
                       stored_currency: str = "") -> PaymentOutcome:
        """UNSIGNED webhook → do NOT trust the body's status. Take only transactionId and RE-QUERY
        TuwaiqPay (authenticated); the fetched status is the truth.

        `stored_currency` (payments-003): the verify-by-fetch call had no currency, so an unusual
        status-response currency shape made _iso_currency return '' and apply_outcome refused a REAL
        success as unverifiable. The caller passes the server-authoritative stored currency so the
        fetch has a correct fallback for THIS payment."""
        try:
            payload = json.loads(body.decode("utf-8", "replace")) if body else {}
        except Exception:   # noqa: BLE001
            return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
        details = payload.get("transactionDetails") or {}
        tx_id = str(details.get("transactionId", ""))
        if not tx_id:
            return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
        # payments-007: do NOT swallow an unreachable gateway into a verified=False PENDING — that is
        # indistinguishable from "verified, nothing to do" and makes the route 200 the gateway into
        # NEVER retrying, silently dropping the payment. Let GatewayUnavailable propagate so the webhook
        # route can 503 and the gateway retries (the reconcile sweep is the backstop).
        confirmed = self.fetch_status(provider_ref=tx_id, currency=stored_currency)
        # verified only for a settled outcome our own GET confirmed; a still-pending webhook is not
        # actionable. The gateway's numeric detail id is the per-notification dedupe key.
        confirmed.verified = confirmed.status in (SUCCEEDED, FAILED, CANCELLED)
        confirmed.event_id = str(details.get("id", ""))
        return confirmed
