"""Rate-limit POLICY — how a request becomes a key, a class, and a cost.

The limiter (the port) answers "is this key over its budget". The policy decides *what the key is*
and *which budget applies*, and both are things a platform must be able to configure without a code
change.

KEY (phase-32 T7). Keying on IP alone is the obvious choice and the wrong one: behind a single NAT
that is one bucket for a whole office, and one enthusiastic user throttles their colleagues. The key here is COMPOSITE and configured:

    RATELIMIT_KEY_PARTS = "subject,ip"

Each listed part contributes if it is present. An authenticated request keys on the subject (fair —
each human their own budget); an anonymous one falls through to the IP (all we have). Add `tenant`
to give each tenant a budget, `route` to budget each endpoint class separately.

CLASS + COST. Not every endpoint deserves the same budget. A login surface must be far stricter
than a health check, and a search that fans out to five services should cost more than one token.
Route classes and their limits/windows/costs are config:

    RATELIMIT_ROUTES = '[{"prefix":"/api/v1/auth","class":"auth","limit":10,"window_s":60,"cost":1}]'

The first matching prefix (longest first) wins; anything unmatched gets the default class.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class RouteClass:
    name: str
    prefix: str
    limit: int
    window_s: int
    cost: int


class RateLimitPolicy:
    def __init__(self, *, key_parts: tuple[str, ...], routes: tuple[RouteClass, ...],
                 default: RouteClass) -> None:
        # Longest prefix first: a short prefix declared first must not swallow the longer, stricter
        # ones behind it — the same shadowing trap the edge renderer sorts away.
        self._routes = tuple(sorted(routes, key=lambda r: len(r.prefix), reverse=True))
        self._default = default
        self._key_parts = key_parts

    @classmethod
    def from_config(cls, *, key_parts: str, routes_json: str,
                    default_limit: int, default_window_s: int) -> "RateLimitPolicy":
        parts = tuple(p.strip() for p in key_parts.split(",") if p.strip())
        if not parts:
            # An empty key is one global bucket for everyone — a self-inflicted DoS the first time
            # any client is busy. Refuse it at boot rather than discover it in production.
            raise ValueError("RATELIMIT_KEY_PARTS is empty — every caller would share one budget")
        routes = []
        for r in json.loads(routes_json or "[]"):
            routes.append(RouteClass(
                name=r.get("class", r["prefix"]), prefix=r["prefix"],
                limit=int(r["limit"]), window_s=int(r["window_s"]), cost=int(r.get("cost", 1))))
        default = RouteClass("default", "", default_limit, default_window_s, 1)
        return cls(key_parts=parts, routes=tuple(routes), default=default)

    def classify(self, path: str) -> RouteClass:
        for r in self._routes:
            if path.startswith(r.prefix):
                return r
        return self._default

    def key(self, cls: RouteClass, *, subject: str | None, ip: str | None,
            tenant: str | None) -> str:
        """Build the composite key from the configured parts that are actually present.

        The class name is always in the key, so the `auth` budget and the `api` budget for the same
        subject are separate counters — a user hammering login does not spend their read budget, and
        vice versa.
        """
        parts: list[str] = []
        for part in self._key_parts:
            if part == "subject" and subject:
                parts.append(f"s:{subject}")
            elif part == "ip" and ip:
                parts.append(f"i:{ip}")
            elif part == "tenant" and tenant:
                parts.append(f"t:{tenant}")
            elif part == "route":
                parts.append(f"r:{cls.name}")
        if not parts:
            # None of the configured identifiers were present (e.g. key_parts=subject on an
            # anonymous request). Fall back to the IP so the request is still bucketed, never
            # unbucketed — an unbucketed request is an unlimited one.
            parts = [f"i:{ip}" if ip else "i:unknown"]
        return f"{cls.name}|" + "|".join(parts)
