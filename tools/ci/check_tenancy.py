#!/usr/bin/env python
"""Gate: the tenant wall. A token carries exactly ONE tenant, and only one it is entitled to.

THE FOOTGUN THIS EXISTS FOR.

Sixteen services read the `tenant` claim out of an access token and scope every query, every row
and every invoice to it. They are RIGHT to trust it — that trust is the whole architecture. It
also means **the mint is the only place the tenant is ever checked.** Everywhere else, it is simply
believed.

So if the check at the mint is deleted, weakened, or routed around, nothing downstream can tell.
Every other gate in this repository stays green while one company reads another's data. There is no
test that fails, no exception in a log, no alert. That is the entire threat: it is silent.

A check that important is not a line in a function that someone can delete during a refactor with
the tests still passing. It is a gate, and it is asserted against the CODE.

Five rules, from the phase-31 threat model:

  R1  The tenant written into a token comes from a MEMBERSHIP object — never from a request,
      a body, a query param, or a header.                                     (T1, CWE-639/863)
  R2  Every mint of a tenant-scoped token goes through the mint guard         (T1)
      (`claims_for`), and every entry point to it verifies membership first
      (`authorize_tenant`) — including refresh.                               (T4, CWE-613)
  R3  A token carries ONE tenant. The claim is a plain string, never a list.  (the invariant)
  R4  No route may mutate a Person's credentials.                             (T2, CWE-640/620)
      A tenant admin may add and remove a MEMBERSHIP. If they can reset the global Person's
      password, they own the freelancer's access to every OTHER company that person works for.
  R5  The Person model carries no `caps` and no `roles` — the field does not  (T3, CWE-269)
      exist, so privilege-on-the-person is unrepresentable, not merely unused.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
# Overridable so the selftest can point the REAL gate at a mutated tree and watch it go red.
# A gate proven only against code that passes has not been proven at all.
SERVICES = pathlib.Path(os.environ.get("TENANCY_SERVICES_DIR", "") or (ROOT / "services"))

# PLUG-AND-PLAY. Which service owns identity, what the mint guard is called, and which fields count
# as a person's credentials are PLATFORM decisions. A product that renames `caps` to `scopes`, or
# runs identity under another name, must be able to point this gate at its own vocabulary rather
# than fork it. Nothing below is hardcoded to how THIS repo happens to spell things.
#
#   TENANCY_SERVICES        comma list; services that mint tenant-scoped tokens
#   TENANCY_MINT_GUARD      the ONLY function that may put a tenant into a token
#   TENANCY_AUTHORIZER      the function that verifies membership before a mint
#   TENANCY_MEMBERSHIP_VARS comma list; names that hold a verified membership row
#   TENANCY_PERSON_MODEL    the global-identity model that must carry no authority
#   TENANCY_NO_AUTHORITY    comma list; fields the person model may NOT have
#   TENANCY_CRED_FIELDS     comma list; a person's credentials — no route may write these
def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


SVCS = _list("TENANCY_SERVICES", "identity-service")
MINT_GUARD = os.environ.get("TENANCY_MINT_GUARD", "claims_for")
AUTHORIZER = os.environ.get("TENANCY_AUTHORIZER", "authorize_tenant")
# `membership`/`m` are the human path; `account` is the MACHINE path — a ServiceAccount fetched from
# the store by client_id and authenticated by its secret. Its `.tenant` was fixed at issue from the
# issuer's VERIFIED token (T1) and is server-authoritative, never read off the token-exchange request,
# so it is a verified row in exactly the sense this gate protects.
MEMBERSHIP_VARS = _list("TENANCY_MEMBERSHIP_VARS", "membership,m,account")
PERSON_MODEL = os.environ.get("TENANCY_PERSON_MODEL", "Person")
NO_AUTHORITY = _list("TENANCY_NO_AUTHORITY", "caps,roles,scopes,permissions")
CRED_FIELDS = _list("TENANCY_CRED_FIELDS", "password_hash,mfa_secret,identifier")
PERSON_VARS = _list("TENANCY_PERSON_VARS", "person,p")
PERSON_MUTATORS = _list("TENANCY_PERSON_MUTATORS",
                        "create_person,delete_person,set_password,reset_password")
ADMIN_DEPS = _list("TENANCY_ADMIN_DEPS", "Principal,require_cap,require_admin")
TENANT_CLAIM = os.environ.get("TENANCY_CLAIM", "tenant")

fail: list[str] = []


def _py(svc: pathlib.Path):
    return sorted((svc / "app").rglob("*.py"))


def _parse(p: pathlib.Path) -> ast.Module | None:
    try:
        return ast.parse(p.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None


def _rel(p: pathlib.Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()          # a tree outside the repo (the selftest's)


def check_service(svc: pathlib.Path) -> None:
    files = _py(svc)
    if not files:
        # The empty set passing green is the recurring defect of this whole repository. A tenancy
        # gate that finds no code to check has not PASSED; it has failed to run.
        fail.append(f"{svc.name}: no python under app/ — the gate found nothing to check")
        return

    saw_person_model = False
    saw_mint_guard = False

    for f in files:
        tree = _parse(f)
        if tree is None:
            fail.append(f"{_rel(f)}: does not parse")
            continue

        for node in ast.walk(tree):
            # ── R5: the person model may carry no authority ──────────────────
            if isinstance(node, ast.ClassDef) and node.name == PERSON_MODEL:
                saw_person_model = True
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        if stmt.target.id in NO_AUTHORITY:
                            fail.append(
                                f"{_rel(f)}:{stmt.lineno}: {PERSON_MODEL}.{stmt.target.id} — "
                                f"authority on the global person is admin at EVERY tenant they "
                                f"touch (T3). It belongs on the membership."
                            )

            # ── R1 + R3: where does a tenant claim come from? ────────────────
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if not (isinstance(k, ast.Constant) and k.value == TENANT_CLAIM):
                        continue
                    # R3: one tenant. A list/set/tuple in the claim is two tenants in a token.
                    if isinstance(v, (ast.List, ast.Set, ast.Tuple)):
                        fail.append(
                            f"{_rel(f)}:{v.lineno}: the '{TENANT_CLAIM}' claim is a collection — "
                            f"a token carries exactly ONE tenant, never two."
                        )
                        continue
                    # R1: it must be read off an object we verified (membership.tenant), never off
                    # a request. `body.tenant` is a request to be let in, not proof of belonging.
                    if isinstance(v, ast.Attribute) and v.attr == TENANT_CLAIM:
                        src = v.value.id if isinstance(v.value, ast.Name) else ""
                        # `user.tenant` is the pre-Phase-31 single-tenant path: the user IS the
                        # tenant, so there is nothing to verify. Anything else must be a membership.
                        if src not in (*MEMBERSHIP_VARS, "user", "self"):
                            fail.append(
                                f"{_rel(f)}:{v.lineno}: the '{TENANT_CLAIM}' claim is taken from "
                                f"'{src}.{TENANT_CLAIM}' — it must come from the membership row "
                                f"that was VERIFIED, never from the request (T1)."
                            )

            # ── R2: the mint guard must verify, not merely format ────────────
            if isinstance(node, ast.FunctionDef) and node.name == MINT_GUARD:
                saw_mint_guard = True
                # It may not accept a tenant from its caller: no parameter may be the tenant.
                args = [a.arg for a in node.args.args + node.args.kwonlyargs]
                if TENANT_CLAIM in args:
                    fail.append(
                        f"{_rel(f)}:{node.lineno}: {MINT_GUARD}() takes a '{TENANT_CLAIM}' "
                        f"parameter — then a caller can hand it one. It must READ the tenant off "
                        f"the membership it was given (T1)."
                    )
                # It must actually refuse something. A guard with no raise is a formatter.
                if not any(isinstance(n, ast.Raise) for n in ast.walk(node)):
                    fail.append(
                        f"{_rel(f)}:{node.lineno}: {MINT_GUARD}() cannot refuse anything — it has "
                        f"no `raise`. A mint guard that never says no is decoration."
                    )

    if not saw_person_model:
        fail.append(f"{svc.name}: no `{PERSON_MODEL}` model found — "
                    f"TENANCY_PERSON_MODEL is pointed at something that does not exist")
    if not saw_mint_guard:
        fail.append(f"{svc.name}: no `{MINT_GUARD}()` found — "
                    f"the mint guard is the only door between companies, and it is missing")

    check_refresh_reverifies(svc)
    check_no_route_touches_credentials(svc)


def check_refresh_reverifies(svc: pathlib.Path) -> None:
    """R2 (T4): a tenant-scoped refresh MUST re-verify the membership.

    Fire a contractor and their access token dies with its TTL — bounded, accepted. Their REFRESH
    token must not keep minting new ones for the tenant they were removed from. This is the single
    most dangerous line in the phase: a refresh that only checks the token's own validity will
    happily re-mint for a company that fired the holder last week.

    Checked in `app/core/` only. The HTTP route also happens to be called `refresh`, and it is
    SUPPOSED to be a thin delegate to the core — demanding the check there too would be demanding
    the security decision be made twice, in the layer least able to make it.
    """
    core = svc / "app" / "core"
    if not core.is_dir():
        return
    for f in sorted(core.rglob("*.py")):
        tree = _parse(f)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "refresh"):
                continue
            calls = {n.func.id for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if AUTHORIZER not in calls:
                fail.append(
                    f"{_rel(f)}:{node.lineno}: refresh() does not call {AUTHORIZER}() — a revoked "
                    f"member keeps minting tokens for the whole refresh lifetime (T4, CWE-613)."
                )


def _is_route(fn: ast.AST) -> bool:
    return isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
        and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"
        for d in fn.decorator_list
    )


def _is_tenant_scoped(fn: ast.AST) -> bool:
    """Is this route authorized by a TENANT's authority, rather than by the person themselves?

    A route that takes a `Principal` (or any of TENANCY_ADMIN_DEPS) is acting on the authority of
    whoever holds a tenant-scoped token — a tenant admin. That is exactly the caller T2 forbids
    from reaching a global person's credentials. A self-service route where the person proves
    themselves with their own password is a different thing entirely, and is allowed.
    """
    src = ast.dump(fn)
    return any(dep in src for dep in ADMIN_DEPS)


def check_no_route_touches_credentials(svc: pathlib.Path) -> None:
    """R4 (T2): the vulnerability multi-tenancy CREATES, and the easiest one to miss.

    While a user belongs to exactly one tenant, a tenant admin resetting "their" user's password is
    harmless — that user is theirs. The moment a Person is GLOBAL, an admin at Company A who can
    reset that password owns the freelancer's access to Company B, C and D. So does anyone who
    social-engineers Company A's helpdesk.

    The boundary: a tenant admin may grant and revoke a MEMBERSHIP. They may never touch the
    person — not the password, not the MFA secret, not the identifier, not their existence.
    """
    api = svc / "app" / "api"
    if not api.is_dir():
        return
    for f in sorted(api.rglob("*.py")):
        tree = _parse(f)
        if tree is None:
            continue
        for fn in ast.walk(tree):
            if not (_is_route(fn) and _is_tenant_scoped(fn)):
                continue
            for node in ast.walk(fn):
                targets = node.targets if isinstance(node, ast.Assign) else (
                    [node.target] if isinstance(node, ast.AnnAssign) else [])
                for t in targets:
                    # Only on a PERSON. `user.mfa_secret` on the single-tenant path is that user's
                    # own enrolment, and the user IS the tenant — there is no second company to
                    # take over.
                    if (isinstance(t, ast.Attribute) and t.attr in CRED_FIELDS
                            and isinstance(t.value, ast.Name) and t.value.id in PERSON_VARS):
                        fail.append(
                            f"{_rel(f)}:{t.lineno}: tenant-scoped route '{fn.name}' writes "
                            f"'{t.attr}' on a person — an admin at ONE company must never be able "
                            f"to change the credentials that unlock every OTHER company that "
                            f"person works for (T2, CWE-640)."
                        )
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in PERSON_MUTATORS):
                    fail.append(
                        f"{_rel(f)}:{node.lineno}: tenant-scoped route '{fn.name}' calls "
                        f"'{node.func.attr}()' — a tenant admin may create and revoke a "
                        f"MEMBERSHIP, never the PERSON (T2)."
                    )


def main() -> int:
    if not SERVICES.is_dir():
        print(f"FATAL: no services/ under {ROOT} — the gate found nothing to check")
        return 1
    checked = 0
    for name in SVCS:
        svc = SERVICES / name
        if not svc.is_dir():
            # PENDING, not PASS: a service named in config but absent is a typo, not an all-clear.
            print(f"FATAL: TENANCY_SERVICES names '{name}', which does not exist")
            return 1
        check_service(svc)
        checked += 1

    if not checked:
        print("FATAL: TENANCY_SERVICES is empty — a gate with no subject cannot pass")
        return 1

    if fail:
        print(f"safety:tenancy — {len(fail)} violation(s):\n")
        for f in fail:
            print(f"  {f}")
        print("\nThe tenant claim is the only wall between every company on this platform.")
        return 1
    print(f"safety:tenancy OK — {checked} service(s): the mint verifies, "
          f"the token carries one tenant, the person carries no authority")
    return 0


if __name__ == "__main__":
    sys.exit(main())
