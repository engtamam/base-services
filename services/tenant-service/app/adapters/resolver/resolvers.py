"""Domain resolvers — map a host to a tenant slug."""
from __future__ import annotations

from app.ports.datastore import DataStore

_DOMAIN_INDEX = "tenant_domains"  # host -> {slug}


def normalize_host(host: str) -> str:
    return (host or "").strip().lower().split(":")[0]  # drop port


class SubdomainResolver:
    """`<slug>.<BASE_DOMAIN>` → slug."""
    driver = "subdomain"

    def __init__(self, base_domain: str) -> None:
        self._base = normalize_host(base_domain)

    async def tenant_slug_for(self, host: str) -> str | None:
        host = normalize_host(host)
        suffix = "." + self._base
        if not host.endswith(suffix):
            return None
        label = host[: -len(suffix)]
        # only the left-most label is the tenant slug; reject empty/multi-level
        if not label or "." in label:
            return None
        return label


class CustomDomainResolver:
    """Look up a mapped custom domain in the store; fall back to subdomain."""
    driver = "custom-domain"

    def __init__(self, store: DataStore, base_domain: str) -> None:
        self._store = store
        self._fallback = SubdomainResolver(base_domain)

    async def tenant_slug_for(self, host: str) -> str | None:
        host = normalize_host(host)
        row = await self._store.get(_DOMAIN_INDEX, host)
        if row and row.get("slug"):
            return row["slug"]
        return await self._fallback.tenant_slug_for(host)
