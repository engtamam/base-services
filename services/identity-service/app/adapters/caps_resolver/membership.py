"""MembershipCaps — capabilities read straight off the membership row.

The behaviour identity has always had, and the default: it needs no authz-service, so a small
deployment that does not run one still works. Roles are carried into the token untouched (a
service may still read the `roles` claim) but they GRANT nothing — only `caps` do.

That limitation is the reason the `authz` driver exists: here, editing a role later changes
nothing for anybody who already holds it.
"""
from __future__ import annotations


class MembershipCaps:
    driver = "membership"

    async def resolve(self, *, subject: str, tenant: str, roles: list[str],
                      caps: list[str]) -> list[str]:
        return list(caps)
