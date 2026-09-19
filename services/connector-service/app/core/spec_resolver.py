"""Rehydrating a provider spec, including the OAuth client secret it authenticates with.

THIS LIVES IN CORE RATHER THAN IN THE API LAYER, DELIBERATELY. The resolved spec carries a
plaintext `client_secret`, because an OAuth token exchange cannot be made with a masked one. A dict
holding a live credential that sits inside a route module is one `return` away from being a
response, and that return would look entirely reasonable in review — it is just the spec the
handler already has.

So the boundary is mechanical: `safety:no-secret-reads` fails the build if an API module reads a
stored credential's plaintext at all. Adapters and core may; routes may not. Moving the resolution
here is what satisfies that rule, and the rule is what stops the mistake.
"""
from __future__ import annotations

from typing import Any

from app.core.models import Connection
from app.core.stores import SpecStore
from app.core import seal as _seal


async def resolve_spec(datastore, secret_store, settings, conn: Connection) -> dict[str, Any]:
    """The provider spec for this connection, with its client secret unsealed in memory only.

    An operator can rotate the OAuth client secret from the panel without re-submitting the whole
    spec — a spec rewrite is where a token URL or a field map gets clobbered by accident. What is
    stored WINS over what the spec carries; with nothing stored this is exactly the behaviour
    before operator-managed credentials existed, so no deployment has to migrate anything.
    """
    spec = await SpecStore(datastore).get(conn.spec_id)
    if spec is None:
        return {}
    out = spec.to_dict()
    out.pop("client_secret_sealed", None)
    out["client_secret"] = _seal.unseal(spec.client_secret_sealed, settings.seal_key,
                                        _seal.spec_secret_aad(spec.id)) or ""
    if secret_store is not None:
        out["client_secret"] = await secret_store.get(
            tenant=conn.tenant,
            name=f"{settings.OPERATOR_SPEC_SECRET_PREFIX}{spec.id}",
            fallback=out["client_secret"])
    if conn.instance_url:
        out["base_url"] = conn.instance_url
    return out
