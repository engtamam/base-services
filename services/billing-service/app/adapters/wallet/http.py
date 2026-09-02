"""wallet-service over HTTP — POST /internal/wallet/credit.

THE HOST IS CHECKED AGAINST THE EGRESS ALLOWLIST, and that is not ceremony. This URL names an
internal service and travels with an internal token in the header; a wrong or attacker-influenced
host receives that token and every tenant's credit traffic. The allowlist is what makes the
destination a decision someone made rather than whatever the config happened to contain.

RETRYABLE AND PERMANENT ARE DISTINGUISHED BY STATUS. A 5xx or a transport failure is worth retrying
until it works. A 400 never will be — a fund that does not exist will not exist on the thousandth
attempt, and retrying for ever turns a misconfiguration into a queue nobody can drain.

The credit itself is idempotent on `source` at the far side, so a retry after an ambiguous failure
— the request landed but the response was lost — credits once, not twice.
"""
from __future__ import annotations

import urllib.parse

from app.adapters.billing_http import post_json
from app.core.internal_client import assertion_headers_from_env
from app.ports.wallet import WalletRefused, WalletUnavailable


class HttpWallet:
    driver = "http"

    def __init__(self, base_url: str, token_provider, *, allowed_hosts: frozenset[str],
                 timeout_seconds: int) -> None:
        if not base_url:
            raise ValueError("WALLET_SERVICE_URL is required for the http wallet driver")
        host = (urllib.parse.urlparse(base_url).hostname or "").lower()
        if allowed_hosts and host not in allowed_hosts:
            # Refused at construction, not at the first credit. A destination the operator did not
            # allow is a boot-time mistake, and finding it at boot is the difference between a
            # failed deploy and an internal token posted somewhere unintended.
            raise ValueError(
                f"WALLET_SERVICE_URL host {host!r} is not in EGRESS_ALLOWED_HOSTS — this URL "
                f"carries the internal service token")
        self._base = base_url.rstrip("/")
        # A callable, not a captured string: the token is an operator-managed credential and may
        # be rotated while this process is running.
        self._token = token_provider
        self._timeout = timeout_seconds

    async def credit(self, *, tenant: str, fund: str, amount_minor: int, source: str,
                     reason: str = "", expires_at: float | None = None) -> bool:
        token = await self._token()
        status, body = post_json(
            f"{self._base}/internal/wallet/credit",
            {"tenant": tenant, "fund": fund, "amount": int(amount_minor),
             "source": source, "reason": reason,
             **({"expires_at": expires_at} if expires_at else {})},
            {"X-Internal-Token": token, **assertion_headers_from_env(target="wallet-service")},
            timeout=self._timeout,
        )
        if 200 <= status < 300:
            return True
        if status in (400, 404, 422):
            # The body is not echoed: it comes from another service and may name funds, tenants or
            # limits that do not belong in this service's logs.
            raise WalletRefused(f"wallet refused the credit (status {status})")
        raise WalletUnavailable(f"wallet unavailable (status {status})")

    async def debit(self, *, tenant: str, fund: str, amount_minor: int, source: str,
                    reason: str = "", allow_negative: bool = False) -> bool:
        token = await self._token()
        status, body = post_json(
            f"{self._base}/internal/wallet/debit",
            {"tenant": tenant, "fund": fund, "amount": int(amount_minor),
             "source": source, "reason": reason, "allow_negative": bool(allow_negative)},
            {"X-Internal-Token": token, **assertion_headers_from_env(target="wallet-service")},
            timeout=self._timeout,
        )
        if 200 <= status < 300:
            return True
        # 409 = insufficient balance with allow_negative off. Permanent for THIS clawback: the
        # caller records grant.clawback_failed rather than retrying a balance that will not grow
        # back on its own. A fund/tenant that does not exist (400/404/422) is permanent too.
        if status in (400, 404, 409, 422):
            raise WalletRefused(f"wallet refused the debit (status {status})")
        raise WalletUnavailable(f"wallet unavailable (status {status})")

    async def health(self) -> bool:
        return True
