"""Isolation strategies — how a tenant's data is separated (peers consume this)."""
from __future__ import annotations

from app.ports.errors import ConfigError


class SharedRowIsolation:
    """One shared store; rows scoped by tenant id (a WHERE tenant_id = ... filter)."""
    driver = "shared-row"

    def scope(self, tenant_id: str, slug: str) -> dict[str, str]:
        return {"mode": "shared-row", "namespace": tenant_id}


class SchemaIsolation:
    """One database, one schema per tenant."""
    driver = "schema"

    def scope(self, tenant_id: str, slug: str) -> dict[str, str]:
        return {"mode": "schema", "schema": f"t_{slug}"}


class DbIsolation:
    """One database per tenant, from a DSN template containing {slug}."""
    driver = "db"

    def __init__(self, dsn_template: str) -> None:
        if not dsn_template or "{slug}" not in dsn_template:
            raise ConfigError("ISOLATION_DB_DSN_TEMPLATE must contain '{slug}' for db isolation")
        self._tmpl = dsn_template

    def scope(self, tenant_id: str, slug: str) -> dict[str, str]:
        return {"mode": "db", "dsn": self._tmpl.format(slug=slug)}
