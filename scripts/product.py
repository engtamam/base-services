"""Read `config/product.json` and turn product identity into per-service env values.

WHERE THIS IS WIRED, AND WHY THERE.

Not into the services. A service reads env vars and nothing else — that is what keeps it
self-contained and separately deployable. Instead this runs at GENERATION time:

    config/product.json  ->  deploy/services/<svc>.values.yaml  ->  ConfigMap  ->  env var

The chart already renders `.Values.env` into a ConfigMap, and `tests/deploy/test_chart_guards.py`
already imports each service's `Settings` and fails on any env key that is not a real field. So a
mapping to a knob that does not exist cannot ship — the guard that catches a typo'd `MAX_GROUP`
catches a typo'd product mapping too. Nothing new had to be invented to make this safe.

EVERY MAPPING BELOW POINTS AT A KNOB THAT EXISTS TODAY. That is the whole discipline: a config
value with no consumer is inert, documented, and wrong — the exact defect `coverage:services`
was written to catch. When I first drafted this map it had `JWT_ISSUER`, `USER_AGENT`, `EVENT_NS`
and a zatca seller block. None of those knobs exist. They are not here.

Two of these come from the FROZEN block, and are frozen for different reasons:

  * `TOKEN_ISSUER` <- identifiers.jwtIssuer
        Every access and refresh token carries the issuer. Change it and every live session is
        rejected on the next request.

  * `ROLLOUT_SALT` <- identifiers.rolloutSalt
        The salt feeds the rollout hash. Change it and every user re-buckets: someone in the 10%
        seeing a new checkout flow is silently moved out of it, and someone who never saw it is
        moved in. No error, no alert. It defaulted to the literal string "base-services" — the
        product name, hardcoded in a service, which is precisely what this file removes.

`MFA_ISSUER_NAME` is the label in an authenticator app. It is NOT frozen: it comes from
`identity.name`, and renaming the product should rename it. Existing TOTP codes keep working —
only the label changes.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "product.json"
EXAMPLE = ROOT / "config" / "product.example.json"

# service -> {ENV_VAR: dotted path into product.json}
# Every ENV_VAR here must be a field on that service's Settings, or the chart guard fails.
WIRING: dict[str, dict[str, str]] = {
    "audit-service": {
        "PII_SALT": "identifiers.piiSalt",   # frozen: re-maps every pseudonym in the chain
    },
    "search-service": {
        "PII_SALT": "identifiers.piiSalt",   # frozen: re-maps every pseudonym
    },
    "file-service": {
        "PII_SALT": "identifiers.piiSalt",   # frozen: re-maps every pseudonym
    },
    "compliance-service": {
        "PII_SALT": "identifiers.piiSalt",   # frozen: re-maps every pseudonym
    },
    "authz-service": {
        "PII_SALT": "identifiers.piiSalt",   # frozen: re-maps every pseudonym
    },
    "analytics-service": {
        "PII_SALT": "identifiers.piiSalt",   # frozen: re-maps every pseudonym
    },
    "identity-service": {
        "PII_SALT": "identifiers.piiSalt",
        "TOKEN_ISSUER": "identifiers.jwtIssuer",       # frozen: rejects every live token
        "MFA_ISSUER_NAME": "identity.name",            # not frozen: just the label in the app
    },
    "feature-flag-service": {
        "PII_SALT": "identifiers.piiSalt",
        "ROLLOUT_SALT": "identifiers.rolloutSalt",     # frozen: re-buckets every user
    },
    "billing-service": {
        "CURRENCY": "locale.currency",
    },
    "wallet-service": {
        "PII_SALT": "identifiers.piiSalt",   # frozen: re-maps every pseudonym
    },
    "zatca-service": {
        "CURRENCY": "locale.currency",
        "PII_SALT": "identifiers.piiSalt",   # frozen: re-maps every pseudonym
    },
    "notification-service": {
        "PII_SALT": "identifiers.piiSalt",
        "DEFAULT_LOCALE": "locale.default",
    },
    "tenant-service": {
        "DEFAULT_LOCALE": "locale.default",
    },
}


def load() -> tuple[dict[str, Any], pathlib.Path]:
    """The real config if it exists, else the example. Returns (config, which_file)."""
    path = CONFIG if CONFIG.exists() else EXAMPLE
    return json.loads(path.read_text(encoding="utf-8")), path


def dig(cfg: dict[str, Any], dotted: str) -> Any:
    node: Any = cfg
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def env_for(service: str, cfg: dict[str, Any]) -> dict[str, str]:
    """The env vars this service inherits from product identity.

    An empty value is OMITTED, never emitted as "". A blank `TOKEN_ISSUER` in a ConfigMap
    overrides the service's own default with nothing, which is worse than not setting it: the
    service starts, mints tokens with an empty issuer, and the failure surfaces at the verifier.
    Until product.json is filled, the services keep their own defaults.
    """
    out: dict[str, str] = {}
    for var, dotted in sorted(WIRING.get(service, {}).items()):
        value = dig(cfg, dotted)
        if value is None or str(value).strip() == "":
            continue
        out[var] = str(value)
    return out


def mapped_paths() -> set[str]:
    return {path for mapping in WIRING.values() for path in mapping.values()}
