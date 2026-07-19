"""onboarding-service — the flows this platform runs.

════════════════════════════════════════════════════════════════════════════════════
  COPY A FLOW, CHANGE THE REQUIREMENTS. That is the whole job.
════════════════════════════════════════════════════════════════════════════════════

**Onboarding is not a wizard.** It is a set of REQUIREMENTS evaluated against the world as it is
right now. Nobody is "on step 3 of 7" — they have some requirements satisfied and some not.

That distinction is not stylistic. A wizard breaks on the first real case and every break becomes
skip-logic:

    a user INVITED to an existing org must not see "create an organisation"
    a seller needs KYC, a buyer does not
    billing is connected in billing-service, not by this wizard
    someone abandons halfway and comes back three weeks later
    a business in KSA needs a CR number a B2C user never touches

Requirements handle all five without a single branch. The invited user has ALREADY SATISFIED
"belongs to an org" — there is nothing to skip. Resuming is free because we recompute rather than
remember a cursor. Reordering is free because `blocks` expresses dependency, not sequence.

────────────────────────────────────────────────────────────────────────────────────
THE FIELDS
────────────────────────────────────────────────────────────────────────────────────

    id             stable name. It appears in APIs and audit records — renaming one is a breaking
                   change, so name it for what is TRUE, not for the screen that collects it
                   ("email_verified", never "step_two").

    satisfied_by   WHO OWNS THE TRUTH. `identity.email_verified` means identity-service decides,
                   and this service asks. Onboarding never keeps a second copy of a fact another
                   service owns — a copy drifts, and the drift always favours "satisfied".

    blocks         requirements that cannot be attempted until this one is satisfied. Dependency,
                   not order: two unblocked requirements can be done in either order or at once.

    grants         what completing it GIVES. Config picks the backend, so a platform with no
                   wallet declares `trial_period` and the same flow works.

    once_per       the anti-abuse key. `verified_identity` means the grant is issued once per
                   VERIFIED registration — surviving account deletion, because verification-service
                   keeps that hash through erasure. Anything else can be farmed.

    optional       may be left unsatisfied without blocking completion. Use it for nudges
                   ("upload an avatar"), never for anything another system depends on.

    scope          `user` (each person satisfies it) or `tenant` (once for the organisation — the
                   second member inherits it). Getting this wrong is how every invited colleague
                   gets asked to re-enter the company's tax number.

────────────────────────────────────────────────────────────────────────────────────
FORMAT
────────────────────────────────────────────────────────────────────────────────────

A PURE literal — read with `ast.literal_eval`, so the gate can check it without importing (and
therefore executing) this service. No variables, no f-strings, no computed values.

Validated at BOOT: an unknown `satisfied_by`, a cycle in `blocks`, a dependency on a requirement
that is not in the flow, or a duplicate id all fail there. Each of those otherwise strands a real
person mid-onboarding, which is the worst place to discover a typo.
"""
from __future__ import annotations

from typing import Any

ONBOARDING_FLOWS: list[dict[str, Any]] = [
    {
        "flow": "b2b_self_serve",
        # Which flow a person gets. First match wins, so put the specific ones first.
        "audience": {"account_type": "business"},
        "requirements": [
            {
                "id": "email_verified",
                "satisfied_by": "identity.email_verified",
                "scope": "user",
                "blocks": ["business_verified"],
            },
            {
                "id": "org_exists",
                "satisfied_by": "tenant.exists",
                "scope": "tenant",
            },
            {
                # The gift depends on this, and that dependency is the entire reason
                # verification-service was built first.
                "id": "business_verified",
                "satisfied_by": "verification.business",
                "scope": "tenant",
                "blocks": ["welcome_credit"],
            },
            {
                "id": "welcome_credit",
                "satisfied_by": "local.granted",
                "scope": "tenant",
                "grants": "welcome",
                # NOT once per tenant: a tenant can be deleted and remade. Once per verified
                # registration, which survives that.
                "once_per": "verified_identity",
            },
            {
                "id": "invite_team",
                "satisfied_by": "identity.has_members",
                "scope": "tenant",
                "optional": True,
            },
        ],
    },
    {
        "flow": "b2c_self_serve",
        "audience": {"account_type": "personal"},
        "requirements": [
            {"id": "email_verified", "satisfied_by": "identity.email_verified", "scope": "user"},
            {"id": "profile_complete", "satisfied_by": "identity.profile_complete",
             "scope": "user", "optional": True},
        ],
    },
    {
        "flow": "invited_member",
        # Someone invited into an existing organisation. Note what is ABSENT: no org creation, no
        # business verification, no gift — all of it belongs to the tenant, which already has them.
        # This is not skip-logic; those requirements simply are not this person's.
        "audience": {"joined_by": "invite"},
        "requirements": [
            {"id": "email_verified", "satisfied_by": "identity.email_verified", "scope": "user"},
        ],
    },
]
