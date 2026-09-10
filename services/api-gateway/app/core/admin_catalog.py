"""The admin-catalog aggregator — collect every service's declared admin actions into one view.

Each service declares its own `admin_actions` in its `/internal/manifest` (see the contract at
docs/contracts/admin-actions.schema.json). This module fans out to the configured services, merges
their declarations, and returns only the actions the CALLER is allowed to use. The platform admin
panel renders the result.

Three things this module treats as load-bearing, all from phase-33:

  DISCOVERY IS CONFIG (T2). The list of services to poll comes from `ADMIN_SERVICES`, never from a
  request. A caller cannot make the aggregator fan out to a host of their choosing — that would be
  an SSRF console into the cluster.

  A DECLARATION IS UNTRUSTED (T4). A service (especially a customer's own product service) declares
  its actions, and a hostile or buggy declaration is input, not gospel. Every action is validated
  here — method allow-listed, path relative with no scheme/host — before it is ever returned. An
  action's `path` is always invoked at ITS service's discovered address; a URL a declaration tries
  to smuggle in is dropped.

  IT IS FILTERED BY THE CALLER'S CAPABILITIES (T5). The catalog returns only actions whose
  capability the caller holds. Otherwise it hands anyone who opens the panel the platform's entire
  capability vocabulary and org chart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

# ONE source of truth for capability matching (wildcard + any-of). The gateway's request enforcement
# imports the same module, so what the catalog SHOWS and what the gateway ALLOWS cannot drift.
from app.core.capmatch import holds as _caps_allow

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/{}-]*$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_CAP_RE = re.compile(r"^[a-z][a-z0-9_.:*-]{1,63}$")


@dataclass(frozen=True)
class ServiceTarget:
    name: str
    base_url: str


def parse_targets(raw: str) -> list[ServiceTarget]:
    """Parse `ADMIN_SERVICES` — a comma list of `name=base_url`. Config only; never a request value.

    e.g. `tenant-service=http://tenant-service:8080,billing-service=http://billing-service:8080`
    """
    targets: list[ServiceTarget] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, url = part.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not name or not url:
            raise ValueError(f"ADMIN_SERVICES entry {part!r} must be name=base_url")
        if not (url.startswith("http://") or url.startswith("https://")):
            # The address is ours to set (config), so it must be a full base URL — not a bare host
            # we would have to guess a scheme for.
            raise ValueError(f"ADMIN_SERVICES url {url!r} must start with http:// or https://")
        targets.append(ServiceTarget(name, url.rstrip("/")))
    return targets


def _valid_action(a: Any) -> dict[str, Any] | None:
    """Return a SANITISED action, or None if the declaration is malformed/hostile.

    Defence in depth: the declaring service is supposed to satisfy the schema, but the aggregator
    never trusts that — a third-party service's manifest is untrusted input.
    """
    if not isinstance(a, dict):
        return None
    id_, label = a.get("id"), a.get("label")
    method, path, cap = a.get("method"), a.get("path"), a.get("capability")
    if not (isinstance(id_, str) and _ID_RE.match(id_)):
        return None
    if not (isinstance(label, str) and 0 < len(label) <= 120):
        return None
    if method not in _METHODS:                       # allow-list, not deny-list
        return None
    if not (isinstance(path, str) and _PATH_RE.match(path)) or path.startswith("//"):
        # A scheme, a host, a query, or a protocol-relative `//host` is refused outright. A browser
        # reads `//evil/x` as a host, so it is an open redirect just like an absolute URL (T4).
        return None
    if not (isinstance(cap, str) and _CAP_RE.match(cap)):
        return None
    out = {"id": id_, "label": label, "method": method, "path": path, "capability": cap}
    for opt in ("group",):
        if isinstance(a.get(opt), str):
            out[opt] = a[opt]
    out["confirm"] = bool(a.get("confirm")) or bool(a.get("danger"))
    out["danger"] = bool(a.get("danger"))
    if isinstance(a.get("params"), list):
        out["params"] = [p for p in a["params"] if isinstance(p, dict) and isinstance(p.get("name"), str)]
    return out




async def build_catalog(
    targets: list[ServiceTarget],
    caller_caps: frozenset[str],
    fetch_manifest: Callable[[ServiceTarget], Awaitable[dict[str, Any] | None]],
    field: str = "admin_actions",
) -> dict[str, Any]:
    """Fan out, merge, filter. `fetch_manifest` is injected so this stays pure and testable — no
    network here, no clock, no token. A target that fails to answer is skipped and REPORTED, never
    silently dropped (a missing service should be visible, not invisible).

    `field` selects which declaration to aggregate: `admin_actions` (the operator panel) or
    `user_actions` (the end-user app). The validation, capability filtering, and anti-SSRF rules are
    identical — only the manifest key differs."""
    services: list[dict[str, Any]] = []
    unreachable: list[str] = []
    for t in targets:
        try:
            manifest = await fetch_manifest(t)
        except Exception:
            manifest = None
        if manifest is None:
            unreachable.append(t.name)
            continue
        raw = manifest.get(field) or []
        actions = []
        for a in raw:
            v = _valid_action(a)
            if v is None:
                continue
            if not _caps_allow(caller_caps, v["capability"]):
                continue                             # the caller cannot use it -> do not show it
            actions.append(v)
        if actions:
            services.append({"service": t.name, "actions": actions})
    return {"services": services, "unreachable": unreachable}
