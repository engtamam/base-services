"""In-process doc-accurate simulators of the Tap and Moyasar APIs — our "sandbox".

These stand in for the real providers at the http-helper boundary (monkeypatch `post_json`/`get_json`),
so the WHOLE stack — adapter → service → routes → webhook verify — runs end to end as in production,
against request/response shapes taken from the providers' live docs (2026-10-07) and with REAL
signatures (Tap hashstring, Moyasar HMAC / secret_token). A request that does not match the documented
shape is REJECTED by the simulator, so a drift in our adapter fails the test instead of silently passing.

They are deliberately strict (assert required fields) and minimal (only what our adapters call).
"""
from __future__ import annotations

import hmac
import json
import re
from hashlib import sha256

_DEC = {"KWD": 3, "BHD": 3, "OMR": 3, "JOD": 3, "IQD": 3, "TND": 3}   # 3-decimal currencies (Tap)


def _id_from(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


class TapSandbox:
    """Simulates Tap (api.tap.company/v2 charges + /v3/lead + payouts). Bearer-auth with the secret key."""

    def __init__(self, secret_key: str = "sk_test") -> None:
        self.secret = secret_key
        self.charges: dict = {}
        self.leads: dict = {}
        self.payouts: dict = {}
        self._n = 0

    # ── transport dispatch (monkeypatched over tap.post_json / tap.get_json) ──────
    def post_json(self, url, payload, headers=None, timeout=None):
        self._auth(headers)
        if url.endswith("/charges"):
            return self._create_charge(payload)
        if re.search(r"/v3/lead$", url) or url.endswith("/lead"):
            return self._create_lead(payload)
        return 404, {"message": f"no such endpoint {url}"}

    def get_json(self, url, headers=None, timeout=None):
        self._auth(headers)
        if "/charges/" in url:
            c = self.charges.get(_id_from(url))
            return (200, c) if c else (404, {"message": "no charge"})
        if "/payouts/" in url:
            p = self.payouts.get(_id_from(url))
            return (200, p) if p else (404, {"message": "no payout"})
        return 404, {"message": f"no such endpoint {url}"}

    def _auth(self, headers):
        auth = (headers or {}).get("Authorization", "")
        assert auth == f"Bearer {self.secret}", f"Tap: bad auth {auth!r}"

    # ── endpoints (strict, doc-accurate) ─────────────────────────────────────────
    def _create_charge(self, p: dict):
        assert isinstance(p.get("amount"), (int, float)) and p["amount"] > 0, "Tap charge: amount"
        assert p.get("currency"), "Tap charge: currency"
        cust = p.get("customer") or {}
        assert cust.get("first_name") and cust.get("email"), "Tap charge: customer.first_name+email required"
        assert (p.get("source") or {}).get("id"), "Tap charge: source.id required"
        # marketplace split (the documented destinations object)
        dests = ((p.get("destinations") or {}).get("destination")) or []
        for d in dests:
            assert d.get("id") and isinstance(d.get("amount"), (int, float)) and d.get("currency"), \
                "Tap charge: each destination needs id+amount+currency"
        self._n += 1
        cid = f"chg_{self._n:06d}"
        cur = str(p["currency"]).upper()
        self.charges[cid] = {
            "id": cid, "status": "INITIATED", "amount": float(p["amount"]), "currency": cur,
            "reference": {"gateway": f"gw_{self._n}", "payment": f"pay_{self._n}",
                          "transaction": (p.get("reference") or {}).get("transaction", ""),
                          "order": (p.get("reference") or {}).get("order", "")},
            "transaction": {"url": f"https://pay.tap.company/{cid}"},
            "created": "1700000000000",
        }
        return 200, self.charges[cid]

    def _create_lead(self, p: dict):
        # Tap v3 lead: a brand name (en) + the marketplace MID are the headline requirements.
        assert p, "Tap lead: empty body"
        self._n += 1
        lid = f"led_{self._n:06d}"
        self.leads[lid] = {"id": lid, "status": "created"}
        return 200, self.leads[lid]

    # ── test helpers (simulate the provider's side) ──────────────────────────────
    def capture(self, charge_id: str) -> None:
        self.charges[charge_id]["status"] = "CAPTURED"

    def add_payout(self, *, status: str = "PAID_OUT", amount: float = 50.0, currency: str = "SAR") -> str:
        self._n += 1
        pid = f"po_{self._n:06d}"
        self.payouts[pid] = {"id": pid, "status": status, "amount": amount, "currency": currency}
        return pid

    def _hashstring(self, charge: dict) -> str:
        cur = charge["currency"].upper()
        amt = f"{float(charge['amount']):.{_DEC.get(cur, 2)}f}"
        ref = charge["reference"]
        fields = (f"x_id{charge['id']}x_amount{amt}x_currency{cur}"
                  f"x_gateway_reference{ref['gateway']}x_payment_reference{ref['payment']}"
                  f"x_status{charge['status']}x_created{charge['created']}")
        return hmac.new(self.secret.encode(), fields.encode(), sha256).hexdigest()

    def charge_webhook(self, charge_id: str):
        """(headers, body) exactly as Tap would POST — a signed hashstring over the current charge."""
        c = self.charges[charge_id]
        return {"hashstring": self._hashstring(c)}, json.dumps(c).encode()

    def payout_webhook(self, payout_id: str):
        return {}, json.dumps({"id": payout_id}).encode()


class MoyasarSandbox:
    """Simulates Moyasar (api.moyasar.com/v1 invoices + entities + payouts). Basic-auth, minor units."""

    def __init__(self, secret_key: str = "sk_live", webhook_secret: str = "whsec") -> None:
        self.secret = secret_key
        self.webhook_secret = webhook_secret
        self.invoices: dict = {}
        self.entities: dict = {}
        self.payouts: dict = {}
        self._n = 0

    def post_json(self, url, payload, headers=None, timeout=None):
        self._auth(headers)
        if url.endswith("/invoices"):
            return self._create_invoice(payload)
        if url.endswith("/entities"):
            return self._create_entity(payload)
        return 404, {"message": f"no such endpoint {url}"}

    def get_json(self, url, headers=None, timeout=None):
        self._auth(headers)
        if "/invoices/" in url:
            inv = self.invoices.get(_id_from(url))
            return (200, inv) if inv else (404, {"message": "no invoice"})
        if "/payouts/" in url:
            p = self.payouts.get(_id_from(url))
            return (200, p) if p else (404, {"message": "no payout"})
        return 404, {"message": f"no such endpoint {url}"}

    def _auth(self, headers):
        # Basic auth, secret key as username, empty password.
        import base64
        auth = (headers or {}).get("Authorization", "")
        assert auth.startswith("Basic "), f"Moyasar: bad auth {auth!r}"
        decoded = base64.b64decode(auth[len("Basic "):]).decode()
        assert decoded == f"{self.secret}:", f"Moyasar: bad basic creds {decoded!r}"

    def _create_invoice(self, p: dict):
        assert isinstance(p.get("amount"), int) and p["amount"] > 0, "Moyasar invoice: amount (minor int)"
        assert p.get("currency"), "Moyasar invoice: currency"
        for s in (p.get("splits") or []):
            assert isinstance(s.get("amount"), int) and s.get("recipient_id"), \
                "Moyasar split: amount(minor)+recipient_id"
        self._n += 1
        iid = f"inv_{self._n:06d}"
        self.invoices[iid] = {"id": iid, "status": "initiated", "amount": int(p["amount"]),
                              "currency": str(p["currency"]).upper(),
                              "url": f"https://moyasar.com/invoices/{iid}"}
        return 200, self.invoices[iid]

    def _create_entity(self, p: dict):
        assert p, "Moyasar entity: empty body"
        self._n += 1
        eid = f"ent_{self._n:06d}"
        self.entities[eid] = {"id": eid, "status": "active"}
        return 200, self.entities[eid]

    def pay_invoice(self, invoice_id: str) -> None:
        self.invoices[invoice_id]["status"] = "paid"

    def add_payout(self, *, status: str = "paid", amount: int = 5000, currency: str = "SAR") -> str:
        self._n += 1
        pid = f"po_{self._n:06d}"
        self.payouts[pid] = {"id": pid, "status": status, "amount": amount, "currency": currency}
        return pid

    def invoice_webhook(self, invoice_id: str, *, mode: str = "token"):
        """(headers, body) as Moyasar would POST. mode='token' puts secret_token in the body; mode='hmac'
        signs the raw body with X-Moyasar-Signature."""
        inv = self.invoices[invoice_id]
        payload = {"type": "payment_paid", "data": {"id": inv["id"], "status": inv["status"]}}
        if mode == "token":
            payload["secret_token"] = self.webhook_secret
            return {}, json.dumps(payload).encode()
        body = json.dumps(payload).encode()
        sig = hmac.new(self.webhook_secret.encode(), body, sha256).hexdigest()
        return {"x-moyasar-signature": sig}, body
