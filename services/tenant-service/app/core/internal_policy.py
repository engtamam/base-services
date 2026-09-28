"""InternalRoutePolicy — which verified callers may reach which /internal routes.

The second half of least-privilege on the internal tier: identity says WHO is calling, this says
whether they may reach THIS route. Deliberately DEFAULT-OPEN: a route with no declared entry admits
any authenticated caller, so turning caller-identity on never breaks an existing call path by itself —
a deployment tightens route by route as it learns its real caller→route edges.

A route WITH an allow-list admits only the named callers. The unnamed shared-token peer (caller="",
the flat-token fallback) is admitted to such a route ONLY when the list contains "*" (any authenticated
caller) — a NAMED list means a named, assertion-proven caller is required, which is the whole point of
declaring one. Matching is longest-prefix, so a policy on `/internal/whatsapp/templates` also covers
`/internal/whatsapp/templates/{name}/{language}/submit` without enumerating every parameterised path.
"""
from __future__ import annotations


class InternalRoutePolicy:
    def __init__(self, policy: dict[str, list[str]] | None) -> None:
        self._policy: dict[str, set[str]] = {
            k.rstrip("/") or "/": {str(c) for c in (v or [])}
            for k, v in (policy or {}).items() if k}

    def _match(self, path: str) -> set[str] | None:
        """The longest-prefix policy entry for `path`, or None when nothing applies (default-open)."""
        best: str | None = None
        for key in self._policy:
            if path == key or path.startswith(key + "/") or key == "/":
                if best is None or len(key) > len(best):
                    best = key
        return self._policy[best] if best is not None else None

    def permits(self, path: str, caller: str) -> bool:
        allowed = self._match(path)
        if allowed is None:
            return True                       # no policy for this route → any authenticated caller
        if "*" in allowed:
            return True                       # explicitly any authenticated caller (named or unnamed)
        return bool(caller) and caller in allowed

    def covers(self, path: str) -> bool:
        """Whether an explicit policy entry applies to `path` (NOT default-open). Boot guards use this
        to require a destructive route to be deliberately allow-listed (tenant-003)."""
        return self._match(path) is not None
