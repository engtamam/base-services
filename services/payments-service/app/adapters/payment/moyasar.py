"""Moyasar (KSA) marketplace adapter — the SECOND marketplace provider, proving provider-neutrality.

Implements the SAME MarketplaceGateway Protocol as the Tap adapter, against Moyasar's shapes. Every API
detail here is cited, with Verified/UNVERIFIED status + capture date (2026-10-07), in the authoritative
reference docs/integrations/moyasar-marketplace-api-reference.md. Nothing is assumed; the spots still
UNVERIFIED there (entity-creation endpoint; request-side splits on /invoices need account enablement) are
marked inline below. The TRUST ANCHOR for a webhook is verify-by-fetch, like every other adapter here.

Differences from Tap, handled here so the core sees one contract:
  * Basic auth with the platform secret key (not Bearer).
  * amounts are MINOR units already (halalas) — no major-unit conversion.
  * split is a `splits` array of {amount, recipient_id, recipient_type} (platform share = remainder).
  * the webhook carries a consumer-assigned `secret_token` (timing-safe compared) + verify-by-fetch.
"""
from __future__ import annotations

import hmac
import json
import re
from hashlib import sha256
from urllib.parse import quote

from app.adapters.payment_http import basic_auth, get_json, post_json
from app.ports.payment_gateway import (CANCELLED, CAP_CHECKOUT, CAP_PAYOUT, CAP_SPLIT, FAILED,
                                       MODEL_MARKETPLACE, PAYOUT_FAILED, PAYOUT_PAID, PAYOUT_PENDING,
                                       PENDING, SUCCEEDED, CheckoutSession, GatewayUnavailable,
                                       OnboardResult, PaymentOutcome, PayoutStatus, SplitDestination)

_DEFAULT_BASE = "https://api.moyasar.com/v1"
_PAID = {"PAID", "CAPTURED"}
_CANCELLED = {"VOIDED", "CANCELLED"}
_FAILED = {"FAILED", "DECLINED"}
# Moyasar payout statuses (confirmed 2026-10-07): queued, initiated, paid, failed, canceled, returned.
_PAYOUT_PAID = {"PAID"}
_PAYOUT_FAILED = {"FAILED", "CANCELED", "CANCELLED", "RETURNED", "REJECTED"}  # queued/initiated -> pending
_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}\Z")


def _norm(status: str) -> str:
    s = (status or "").upper()
    return (SUCCEEDED if s in _PAID else CANCELLED if s in _CANCELLED
            else FAILED if s in _FAILED else PENDING)


def _norm_payout(status: str) -> str:
    s = (status or "").upper()
    return PAYOUT_PAID if s in _PAYOUT_PAID else PAYOUT_FAILED if s in _PAYOUT_FAILED else PAYOUT_PENDING


class MoyasarGateway:
    driver = "moyasar"
    model = MODEL_MARKETPLACE
    # Moyasar exposes NO entity/recipient-creation API (its documented resource groups — payments,
    # invoices, payouts, settlements, sources, … — have none; ref: docs.moyasar.com/api/api-introduction),
    # so this adapter declares NO onboarding capability: a sub-merchant recipient is created in the Moyasar
    # dashboard, and its id is recorded via /internal/payments/sub-merchants/destination. We never call a
    # non-existent endpoint.
    capabilities = frozenset({CAP_CHECKOUT, CAP_SPLIT, CAP_PAYOUT})
    onboarding_modes: frozenset[str] = frozenset()

    def __init__(self, *, api_key: str = "", base: str = "", webhook_secret: str = "",
                 timeout: int = 30, **_) -> None:
        if not api_key:
            raise GatewayUnavailable("Moyasar needs the platform MARKETPLACE_SECRET_KEY")
        self._secret = api_key
        self._base = (base or _DEFAULT_BASE).rstrip("/")
        self._webhook_secret = webhook_secret
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        # Basic auth: secret key as the username, empty password.
        return {"Authorization": basic_auth(self._secret, "")}

    # ── split checkout ───────────────────────────────────────────────────────────
    def prepare_split_checkout(self, *, amount_minor: int, currency: str, reference: str,
                               destinations: list[SplitDestination], return_url: str = "",
                               metadata: dict | None = None) -> CheckoutSession:
        cur = (currency or "SAR").upper()
        # Moyasar amounts are MINOR units — pass straight through, no conversion (ref: create-invoice).
        # NOTE (ref doc): request-side `splits` on /invoices need account enablement (contact Moyasar);
        # the field shape itself is confirmed on /payments. Posted here for an enabled marketplace account.
        splits = [{"amount": int(d.amount_minor), "recipient_id": d.destination_id,
                   "recipient_type": "Entity"} for d in destinations]
        payload = {
            "amount": int(amount_minor), "currency": cur, "description": reference,
            "splits": splits,
        }
        if return_url:
            payload["success_url"] = return_url
            payload["back_url"] = return_url
        try:
            code, resp = post_json(f"{self._base}/invoices", payload, self._headers(), timeout=self._timeout)
        except GatewayUnavailable as ex:
            return CheckoutSession(ok=False, provider=self.driver, error=str(ex))
        data = resp if isinstance(resp, dict) else {}
        inv_id = str(data.get("id", ""))
        url = str(data.get("url", "") or (data.get("source") or {}).get("transaction_url", ""))
        if code == 0 or not inv_id or not url:
            return CheckoutSession(ok=False, provider=self.driver,
                                   error=f"Moyasar invoice failed: {data.get('message', code)}")
        return CheckoutSession(ok=True, provider=self.driver, provider_ref=inv_id, redirect_url=url,
                               extra={"reference": reference})

    def fetch_status(self, *, provider_ref: str, currency: str = "") -> PaymentOutcome:
        if not _ID_RE.match(provider_ref or ""):
            raise GatewayUnavailable("Moyasar: malformed id")
        ref = quote(provider_ref, safe="")
        code, resp = get_json(f"{self._base}/invoices/{ref}", self._headers(), timeout=self._timeout)
        if code == 0:
            raise GatewayUnavailable("Moyasar unreachable (status)")
        data = resp if isinstance(resp, dict) else {}
        status = _norm(str(data.get("status", "")))
        cur = str(data.get("currency", "") or currency).upper()
        amount_minor = int(data.get("amount", 0) or 0)       # already minor units
        if status == SUCCEEDED and amount_minor <= 0:
            raise GatewayUnavailable("Moyasar: paid invoice with no amount (retry)")
        return PaymentOutcome(status=status, provider=self.driver,
                              provider_ref=str(data.get("id", provider_ref)),
                              amount_minor=amount_minor, currency=cur, verified=True,
                              raw={"status": status})

    def verify_webhook(self, *, headers: dict[str, str], body: bytes,
                       stored_currency: str = "") -> PaymentOutcome:
        """Verified 2026-10-07: Moyasar authenticates a webhook either way —
          * `X-Moyasar-Signature` header = HMAC-SHA256 of the RAW body keyed with our secret (hex), OR
          * a `secret_token` field in the body that EQUALS our secret.
        Whichever is present is compared timing-safe; a present-but-wrong one is refused WITHOUT a fetch.
        The trust anchor is still the verify-by-fetch (re-query the payment). GatewayUnavailable propagates."""
        try:
            data = json.loads(body.decode("utf-8", "replace")) if body else {}
        except Exception:   # noqa: BLE001
            return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
        if not isinstance(data, dict):
            return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
        if self._webhook_secret:
            sig = headers.get("x-moyasar-signature")
            if sig:
                expected = hmac.new(self._webhook_secret.encode(), body or b"", sha256).hexdigest()
                if not hmac.compare_digest(sig, expected):
                    return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
            else:
                token = str(data.get("secret_token", ""))
                if not hmac.compare_digest(token, self._webhook_secret):
                    return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        pay_id = str(inner.get("id", ""))
        if not pay_id:
            return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
        confirmed = self.fetch_status(provider_ref=pay_id, currency=stored_currency)
        confirmed.verified = confirmed.status in (SUCCEEDED, FAILED, CANCELLED)
        return confirmed

    # ── onboarding ───────────────────────────────────────────────────────────────
    def onboard_merchant(self, *, mode: str, data: dict) -> OnboardResult:
        # Moyasar has no public entity/recipient-creation API (see the class docstring), so onboarding is
        # not an API operation here — the recipient is created in the Moyasar dashboard and recorded via
        # /internal/payments/sub-merchants/destination. Refuse rather than call a non-existent endpoint.
        return OnboardResult(
            ok=False, provider=self.driver,
            error="Moyasar has no recipient-creation API — create the recipient (Entity/Beneficiary) in "
                  "the Moyasar dashboard, then record its id via /internal/payments/sub-merchants/destination")

    def fetch_payout_status(self, *, payout_ref: str) -> PayoutStatus:
        if not _ID_RE.match(payout_ref or ""):
            raise GatewayUnavailable("Moyasar: malformed payout id")
        ref = quote(payout_ref, safe="")
        code, resp = get_json(f"{self._base}/payouts/{ref}", self._headers(), timeout=self._timeout)
        if code == 0:
            raise GatewayUnavailable("Moyasar unreachable (payout)")
        data = resp if isinstance(resp, dict) else {}
        cur = str(data.get("currency", "")).upper()
        return PayoutStatus(status=_norm_payout(str(data.get("status", ""))), provider=self.driver,
                            payout_ref=str(data.get("id", payout_ref)),
                            amount_minor=int(data.get("amount", 0) or 0), currency=cur, verified=True,
                            raw={"status": data.get("status", "")})
