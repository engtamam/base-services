"""Erasure — api-gateway: it routes, it does not remember.

The gateway proxies requests and aggregates catalogs. It owns no datastore collection of subject
data: what it holds is routing config and the service catalog, neither of which is about a person.

An endpoint that always answers "nothing here" still earns its place:

  * A DSAR must be able to PROVE it asked every service. "never contacted" and "holds nothing" are
    indistinguishable in an incomplete system and completely different to a regulator.

  * The gateway is the most likely service in the platform to GAIN subject data by accident — a
    request log, a cache of user lookups, a rate-limit bucket keyed by email. When that happens,
    this file is where the decision has to be made, and changing it shows up in review. A service
    with no erase endpoint accumulates PII in silence; one with an endpoint has to lie on purpose.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref


class GatewayErasure:
    service = "api-gateway"
    category = "routing"

    def __init__(self, *, store=None, pii_salt: str) -> None:
        self._store = store       # unused today; present so a future store cannot skip review
        self._salt = pii_salt

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        return {
            "service": self.service,
            "category": self.category,
            "subject_ref": subject_ref(self._salt, tenant, subject),
            "erased": [],
            "retained": [],
            "already_absent": True,
            "note": ("the gateway routes and aggregates; it owns no subject data. Request logs are "
                     "the platform's, not this service's, and carry pseudonyms."),
        }
