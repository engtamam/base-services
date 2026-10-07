"""Tap Payments (MENA) marketplace adapter — split checkout + sub-merchant onboarding + payout status.

Verified against developers.tap.company, captured 2026-10-07 (docs/integrations/tap-marketplace-api-
reference.md). Several shapes there are marked CONFIRM-at-build and are implemented against the documented
structure with the base/endpoints configurable; the TRUST ANCHOR for a webhook is a verify-by-fetch
(Retrieve a Charge), exactly like the TuwaiqPay adapter, so a forged/replayed body can never settle a
payment our own authenticated GET does not confirm.

Marketplace model: the PLATFORM's single secret key (Bearer) authorises a charge that SPLITS across
sub-merchant `destinations`; the platform's commission is the implicit remainder. Credentials come from
the platform-scoped MARKETPLACE_* store, never a per-tenant one.
"""
from __future__ import annotations

import hmac
import json
import re
from hashlib import sha256
from urllib.parse import quote

from app.adapters.payment_http import bearer, get_json, post_json
from app.core.currency import to_major, to_minor
from app.ports.payment_gateway import (CANCELLED, CAP_CHECKOUT, CAP_ONBOARDING, CAP_PAYOUT, CAP_SPLIT,
                                       FAILED, MODEL_MARKETPLACE, ONBOARD_API, ONBOARD_HOSTED,
                                       PAYOUT_FAILED, PAYOUT_PAID, PAYOUT_PENDING, PENDING, SUCCEEDED,
                                       CheckoutSession, GatewayUnavailable, OnboardResult, PaymentOutcome,
                                       PayoutStatus, SplitDestination)

_DEFAULT_BASE = "https://api.tap.company/v2"
# Charge statuses (docs): INITIATED, ABANDONED, CANCELLED, FAILED, DECLINED, RESTRICTED, CAPTURED, VOID,
# TIMEDOUT, UNKNOWN. CAPTURED is the only success.
_PAID = {"CAPTURED"}
_CANCELLED = {"CANCELLED", "VOID", "ABANDONED"}
_FAILED = {"FAILED", "DECLINED", "RESTRICTED", "TIMEDOUT"}
# Payout statuses (docs): PENDING, INITIATED, FAILED, PAID_OUT.
_PAYOUT_PAID = {"PAID_OUT"}
_PAYOUT_FAILED = {"FAILED"}
# An id interpolated into a URL path is pinned to an id-shaped charset so a "../"-style value can never
# traverse (quote() is a second guard) — same L4 discipline as the TuwaiqPay adapter.
_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}\Z")


def _norm_charge(status: str) -> str:
    s = (status or "").upper()
    return (SUCCEEDED if s in _PAID else CANCELLED if s in _CANCELLED
            else FAILED if s in _FAILED else PENDING)


def _norm_payout(status: str) -> str:
    s = (status or "").upper()
    return PAYOUT_PAID if s in _PAYOUT_PAID else PAYOUT_FAILED if s in _PAYOUT_FAILED else PAYOUT_PENDING


class TapGateway:
    driver = "tap"
    model = MODEL_MARKETPLACE
    capabilities = frozenset({CAP_CHECKOUT, CAP_ONBOARDING, CAP_SPLIT, CAP_PAYOUT})
    onboarding_modes = frozenset({ONBOARD_API, ONBOARD_HOSTED})

    def __init__(self, *, api_key: str = "", base: str = "", webhook_secret: str = "",
                 timeout: int = 30, **_) -> None:
        if not api_key:
            raise GatewayUnavailable("Tap needs the platform MARKETPLACE_SECRET_KEY (Bearer)")
        self._secret = api_key
        self._base = (base or _DEFAULT_BASE).rstrip("/")
        self._webhook_secret = webhook_secret
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": bearer(self._secret), "lang_code": "en"}

    # ── split checkout ───────────────────────────────────────────────────────────
    def prepare_split_checkout(self, *, amount_minor: int, currency: str, reference: str,
                               destinations: list[SplitDestination], return_url: str = "",
                               metadata: dict | None = None) -> CheckoutSession:
        meta = metadata or {}
        name = str(meta.get("customerName") or meta.get("customer_name") or "")
        email = str(meta.get("customerEmail") or meta.get("customer_email") or "")
        phone = str(meta.get("customerMobilePhone") or meta.get("customer_mobile") or "")
        if not (name and (email or phone)):
            return CheckoutSession(
                ok=False, provider=self.driver,
                error="Tap requires customerName + (customerEmail or customerMobilePhone) in metadata")
        cur = (currency or "SAR").upper()
        dest = [{"id": d.destination_id, "amount": float(to_major(int(d.amount_minor), cur)),
                 "currency": cur} for d in destinations]
        customer: dict = {"first_name": name}
        if email:
            customer["email"] = email
        if phone:
            customer["phone"] = {"number": phone}
        payload = {
            "amount": float(to_major(int(amount_minor), cur)),
            "currency": cur,
            "customer": customer,
            "source": {"id": "src_all"},
            "reference": {"transaction": reference, "order": reference},
            "destinations": {"destination": dest},
        }
        if return_url:
            payload["redirect"] = {"url": return_url}
        try:
            code, resp = post_json(f"{self._base}/charges", payload, self._headers(), timeout=self._timeout)
        except GatewayUnavailable as ex:
            return CheckoutSession(ok=False, provider=self.driver, error=str(ex))
        data = resp if isinstance(resp, dict) else {}
        charge_id = str(data.get("id", ""))
        url = str(((data.get("transaction") or {}).get("url")) or "")
        if code == 0 or not charge_id or not url:
            return CheckoutSession(ok=False, provider=self.driver,
                                   error=f"Tap charge failed: {data.get('message', code)}")
        return CheckoutSession(ok=True, provider=self.driver, provider_ref=charge_id,
                               redirect_url=url, extra={"reference": reference})

    # ── status / webhook ─────────────────────────────────────────────────────────
    def fetch_status(self, *, provider_ref: str, currency: str = "") -> PaymentOutcome:
        if not _ID_RE.match(provider_ref or ""):
            raise GatewayUnavailable("Tap: malformed charge id")
        ref = quote(provider_ref, safe="")
        code, resp = get_json(f"{self._base}/charges/{ref}", self._headers(), timeout=self._timeout)
        if code == 0:
            raise GatewayUnavailable("Tap unreachable (status)")
        data = resp if isinstance(resp, dict) else {}
        status = _norm_charge(str(data.get("status", "")))
        cur = str(data.get("currency", "") or currency).upper()
        amount_minor = to_minor(data.get("amount", 0) or 0, cur) if cur else 0
        # A SUCCEEDED we cannot price is a transient problem, not a confirmed 0 — reconcile will retry,
        # rather than apply_outcome refusing the real success as an amount mismatch (payments-009 parity).
        if status == SUCCEEDED and amount_minor <= 0:
            raise GatewayUnavailable("Tap: captured charge with unparseable amount (retry)")
        return PaymentOutcome(status=status, provider=self.driver,
                              provider_ref=str(data.get("id", provider_ref)),
                              amount_minor=amount_minor, currency=cur, verified=True,
                              raw={"status": status})

    def _hashstring(self, data: dict) -> str:
        """Tap's webhook hashstring (HMAC-SHA256 over an ordered field concat). The EXACT field order /
        header name is CONFIRM-at-build; the documented order is used here. This is defence-in-depth — the
        trust anchor is the verify-by-fetch below — so a mismatch marks the body untrusted, never settles."""
        cur = str(data.get("currency", "")).upper()
        amt = data.get("amount", 0)
        try:
            amt_fmt = f"{float(to_major(int(to_minor(amt, cur)), cur)):.3f}" if cur else str(amt)
        except Exception:   # noqa: BLE001
            amt_fmt = str(amt)
        fields = (f"x_id{data.get('id','')}"
                  f"x_amount{amt_fmt}"
                  f"x_currency{cur}"
                  f"x_gateway_reference{(data.get('reference') or {}).get('gateway','')}"
                  f"x_payment_reference{(data.get('reference') or {}).get('payment','')}"
                  f"x_status{data.get('status','')}"
                  f"x_created{data.get('transaction',{}).get('created','') if isinstance(data.get('transaction'),dict) else ''}")
        return hmac.new(self._webhook_secret.encode(), fields.encode(), sha256).hexdigest()

    def verify_webhook(self, *, headers: dict[str, str], body: bytes,
                       stored_currency: str = "") -> PaymentOutcome:
        """Trust anchor = re-fetch the charge (body is not fully signed / may be replayed). If a signature
        header AND a webhook secret are present we ALSO compare the hashstring timing-safe as defence in
        depth; a mismatch refuses (verified=False), never settles. GatewayUnavailable propagates so the
        route 503s and Tap retries (reconcile is the backstop) rather than silently dropping a payment."""
        try:
            data = json.loads(body.decode("utf-8", "replace")) if body else {}
        except Exception:   # noqa: BLE001
            return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
        if not isinstance(data, dict):
            return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
        charge_id = str(data.get("id", ""))
        if not charge_id:
            return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
        sig = headers.get("hashstring") or headers.get("signature") or ""
        if self._webhook_secret and sig:
            if not hmac.compare_digest(sig, self._hashstring(data)):
                # A present-but-wrong signature is hostile — refuse without even fetching.
                return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
        confirmed = self.fetch_status(provider_ref=charge_id, currency=stored_currency)
        confirmed.verified = confirmed.status in (SUCCEEDED, FAILED, CANCELLED)
        return confirmed

    # ── onboarding ───────────────────────────────────────────────────────────────
    def onboard_merchant(self, *, mode: str, data: dict) -> OnboardResult:
        """Register a sub-merchant. `api` forwards the KYB data to create a destination and returns its id;
        `hosted` returns a handle for the client-side JS Connect flow (the merchant submits to Tap
        directly; the destination id arrives later via an onboarding callback). CONFIRM the exact
        destination-creation chain (lead/business/merchant) + JS Connect embed contract at build."""
        if mode == ONBOARD_HOSTED:
            # The merchant onboards on Tap (JS Connect); we hold nothing but a pending marker until the
            # callback delivers the destination id. The public/embed key is supplied by the caller/config,
            # never a secret of ours.
            return OnboardResult(ok=True, provider=self.driver, status="pending",
                                 handle=str(data.get("handle", "")),
                                 redirect_url=str(data.get("redirect_url", "")),
                                 extra={"onboarding": "hosted"})
        if mode != ONBOARD_API:
            return OnboardResult(ok=False, provider=self.driver, error=f"unsupported onboarding mode {mode!r}")
        # mode == api: create a destination from the forwarded KYB data; persist ONLY the returned id.
        try:
            code, resp = post_json(f"{self._base}/destination", dict(data), self._headers(),
                                   timeout=self._timeout)
        except GatewayUnavailable as ex:
            return OnboardResult(ok=False, provider=self.driver, error=str(ex))
        rd = resp if isinstance(resp, dict) else {}
        dest_id = str(rd.get("id", ""))
        if code == 0 or not dest_id:
            return OnboardResult(ok=False, provider=self.driver,
                                 error=f"Tap onboarding failed: {rd.get('message', code)}")
        # A freshly created destination is typically not yet payable — treat as pending unless Tap says
        # active, so a split can never route to an unverified account (fail-closed; destination_id() in
        # the sub-merchant store also gates on active).
        status = "active" if str(rd.get("status", "")).lower() in ("active", "approved") else "pending"
        return OnboardResult(ok=True, provider=self.driver, destination_id=dest_id, status=status)

    # ── payout ───────────────────────────────────────────────────────────────────
    def fetch_payout_status(self, *, payout_ref: str) -> PayoutStatus:
        if not _ID_RE.match(payout_ref or ""):
            raise GatewayUnavailable("Tap: malformed payout id")
        ref = quote(payout_ref, safe="")
        code, resp = get_json(f"{self._base}/payouts/{ref}", self._headers(), timeout=self._timeout)
        if code == 0:
            raise GatewayUnavailable("Tap unreachable (payout)")
        data = resp if isinstance(resp, dict) else {}
        cur = str(data.get("currency", "")).upper()
        return PayoutStatus(status=_norm_payout(str(data.get("status", ""))), provider=self.driver,
                            payout_ref=str(data.get("id", payout_ref)),
                            amount_minor=to_minor(data.get("amount", 0) or 0, cur) if cur else 0,
                            currency=cur, verified=True, raw={"status": data.get("status", "")})
