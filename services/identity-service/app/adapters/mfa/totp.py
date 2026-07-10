"""TOTP MFA (pyotp) — RFC 6238 time-based one-time passwords."""
from __future__ import annotations

import pyotp


class TotpMfa:
    driver = "totp"
    enabled = True

    def __init__(self, issuer_name: str = "base-services", valid_window: int = 1) -> None:
        self._issuer = issuer_name
        self._window = valid_window

    def provision(self, account: str) -> dict[str, str]:
        secret = pyotp.random_base32()
        uri = pyotp.totp.TOTP(secret).provisioning_uri(name=account, issuer_name=self._issuer)
        return {"secret": secret, "uri": uri}

    def verify(self, secret: str, code: str) -> bool:
        try:
            # valid_window tolerates minor clock skew (± steps of 30s), from config.
            return pyotp.TOTP(secret).verify(code, valid_window=self._window)
        except Exception:
            return False
