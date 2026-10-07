"""payments-service — auditable actions: what this service must be able to PROVE it did.

════════════════════════════════════════════════════════════════════════════════════
  COPY THIS FILE INTO YOUR SERVICE AND FILL IN THE LIST. That is the whole job.
════════════════════════════════════════════════════════════════════════════════════

Why this file exists at all: audit-service shipped a working, hash-chained log and for months not
one service wrote to it. Every test passed, the chain verified perfectly, and it contained nothing.
An empty audit log is worse than no audit log — it passes a compliance questionnaire and answers
nothing when someone asks "who did this?".

Nothing infers what is auditable. **What nobody declares, nobody logs**, and the failure is silent,
so the list is explicit and a gate (safety:audit-wiring) fails if a service declares nothing.

────────────────────────────────────────────────────────────────────────────────────
WHAT BELONGS HERE — four sources, and most people only think of the first
────────────────────────────────────────────────────────────────────────────────────

  source="admin"     an operator acting through the admin panel. Start from your admin_actions.py.
  source="user"      an end user acting on their own data. Start from your user_actions.py.
  source="internal"  SERVICE-TO-SERVICE calls on /internal/*. THE MOST-MISSED CATEGORY, and often
                     the most important: "this service deleted a person's data because a DSAR told
                     it to" is the single most audit-worthy thing it will ever do, and it appears
                     in no panel and no catalog.
  source="auth"      logins, MFA changes, token refreshes — and FAILED ones. These are pre-auth, so
                     they are deliberately absent from the action catalogs; a burst of failures is
                     the most common first sign of an attack.
  source="system"    scheduled work with no human actor: a retention purge deleted 500 rows, a
                     sweeper cancelled a job. Nobody clicked anything, and it still needs a record.

A catalog is therefore a SEED, not the answer: `auditable ⊇ admin ∪ user`, plus internal, auth and
system. Read your own routes — including every `/internal/*` one — rather than trusting a list.

────────────────────────────────────────────────────────────────────────────────────
THE ONE FIELD TO THINK HARDEST ABOUT: `critical`
────────────────────────────────────────────────────────────────────────────────────

`critical: True` means **the action is REFUSED if it cannot be recorded** (503). Not a severity
label — a behaviour, and one that trades availability for evidence.

Mark it True when an unprovable action is worse than a refused one. That is the test, and it is
usually true for exactly one family of things: **who was given power, and who took data out.**

    permission granted / role changed      an unattributable grant is indistinguishable
                                           from an attacker's
    credential or MFA changed              account takeover looks exactly like this
    data exported / bulk-read              the difference between a customer and a breach
    account suspended / deleted            someone lost access and can dispute it
    another person's data erased           you must be able to prove you did it, and when
    security setting changed               turning off a control must never be quiet

Mark it False for routine work — profile edits, reads, list endpoints. Refusing those during an
audit outage protects nothing and turns a logging blip into a partial platform outage.

If you are unsure: ask whether you would be comfortable telling a regulator "it happened, and we
have no record of it". If not, it is critical.

────────────────────────────────────────────────────────────────────────────────────
THE ACTOR IS ALWAYS A PSEUDONYM
────────────────────────────────────────────────────────────────────────────────────

Pass `subject_ref`, never a name, email or raw user id. An audit log holding raw identities becomes
a PII store in its own right — and one that CANNOT honour an erasure request, because deleting a
row from a hash chain is precisely what breaks the chain. Pseudonymise at the edge and the log
stays both useful and erasable: erase the linkage in identity-service and every entry here becomes
an orphan that names nobody, while the chain still verifies.

────────────────────────────────────────────────────────────────────────────────────
THEN EMIT IT — the other half of the job
────────────────────────────────────────────────────────────────────────────────────

Declaring an action does not record it. At the point the action happens:

    await request.app.state.auditor.record(
        "permission.granted",
        actor=subject_ref,               # a PSEUDONYM — see below
        resource=f"grant:{grant.id}",
        tenant=principal.tenant,
        ip=request.client.host,
        metadata={"capability": cap},
    )

For a `critical` action, let `AuditUnavailable` propagate — the API layer turns it into a 503. That
is the fail-closed decision doing its job, and swallowing it would quietly undo the whole point.

Emitting an action that is NOT in the list below raises: an event nobody declared is invisible to
the CI gate and to the investigator searching the log, so it would be audited in name only.

Where to point it (app/config.py has the full set):

    AUDIT_SINK_DRIVER=http   -> the receipt carries seq + hash; the event is sealed INTO the chain
    AUDIT_SINK_DRIVER=eventbus -> no hot-path cost, but the receipt means PUBLISHED, not recorded
    AUDIT_SINK_DRIVER=null   -> records nothing and says so; production refuses to boot on it

See app/adapters/audit_sink/*.py for the trade-off in full, and app/ports/audit_sink.py for the
wire contract.

────────────────────────────────────────────────────────────────────────────────────
FORMAT
────────────────────────────────────────────────────────────────────────────────────

A PURE literal — no imports, no f-strings, no variables, no computed values. It is read with
`ast.literal_eval`, so the gate can check it without importing (and therefore executing) your
service. A variable reference here breaks the gate, not just this file.

    action    dotted `noun.verb`, past tense: "user.suspended", not "suspend_user". It is a record
              of something that HAPPENED.
    source    admin | user | internal | auth | system   (see above)
    critical  True = refuse the action when it cannot be recorded
    resource  what kind of thing it acts on ("user", "invoice"). Not the id — that goes at runtime.
    why       REQUIRED when critical is True. One line: what an investigator would lose without it.
              If you cannot write this line, the action probably is not critical.
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "operator.secret_set",
        "source": "admin",
        "critical": True,
        "resource": "secret",
        "why": "a credential change is the action that most needs attribution: the previous value "
               "is gone the moment it happens, so an unrecorded rotation cannot be reconstructed, "
               "and an unattributable one is indistinguishable from an attacker's",
    },
    {
        "action": "operator.secret_cleared",
        "source": "admin",
        "critical": True,
        "resource": "secret",
        "why": "clearing a credential silently reverts the service to whatever the environment "
               "holds, which is a behaviour change nobody would otherwise see",
    },
    {
        "action": "operator.config_changed",
        "source": "admin",
        "critical": False,
        "resource": "config",
        "why": "policy changes explain later behaviour — why a sweep deleted more, why a limit "
               "started refusing — and that history is unavailable anywhere else. Not critical: "
               "refusing to change a retention period because the log is down protects nothing",
    },
    {
        "action": "operator.option_changed",
        "source": "admin",
        "critical": False,
        "resource": "option",
        "why": "enabling, disabling or locking a shop's commerce option (e.g. the external pay-link) "
               "changes whether it can take money at all; the history explains why a shop could or "
               "could not check out at a point in time. Not critical: refusing a toggle because the "
               "log is down would strand the option in its current state, which protects nothing",
    },
    {
        "action": "operator.payment_mode_changed",
        "source": "admin",
        "critical": False,
        "resource": "payment_mode",
        "why": "an operator selecting a tenant's payment model (conduit vs marketplace), or forcing one "
               "platform-wide, changes how its payments are routed; the history explains the routing in "
               "effect at a point in time. Not critical: a down log must not strand the current selection",
    },
    {
        "action": "payments.merchant_credential_set",
        "source": "internal",
        "critical": True,
        "resource": "merchant_credential",
        "why": "MERCHANT_* credentials decide WHERE a shop's money is routed (API_BASE) and the key "
               "that authorises the call. Silently repointing them is a money-redirection / supply-"
               "chain move an incident review must attribute — who set which credential for which shop, "
               "and when — so an unrecordable change must not proceed. The value is never logged",
    },
    {
        "action": "payments.merchant_credential_cleared",
        "source": "internal",
        "critical": True,
        "resource": "merchant_credential",
        "why": "clearing a merchant credential disables or reroutes a shop's payments; like setting one "
               "it is a money-path change that must be attributable and cannot proceed unrecorded",
    },
    {
        "action": "payments.request_created",
        "source": "internal",
        "critical": False,
        "resource": "payment_request",
        "why": "minting a hosted pay-link is a money action a human/service triggered; recording the "
               "caller, tenant, reference and amount keeps it attributable even though fulfilment goes "
               "over the bus (payments-013). Non-critical: a down log must not block taking a payment",
    },
    {
        "action": "payments.reconciled",
        "source": "internal",
        "critical": False,
        "resource": "payment_request",
        "why": "reconcile pulls the gateway's status and can move a request to paid; it is a money "
               "action and must be attributable to whoever triggered it (payments-013)",
    },
    {
        "action": "payments.marketplace_credential_set",
        "source": "internal",
        "critical": True,
        "resource": "marketplace_credential",
        "why": "the platform MARKETPLACE_* credentials route EVERY sub-merchant split and hold the key "
               "that authorises the charge — the widest-blast-radius money-path change in the service, so "
               "setting one must be attributable and cannot proceed unrecorded. The value is never logged",
    },
    {
        "action": "payments.marketplace_credential_cleared",
        "source": "internal",
        "critical": True,
        "resource": "marketplace_credential",
        "why": "clearing a marketplace credential disables or reroutes the platform's split payments; like "
               "setting one it is a money-path change that must be attributable and cannot proceed unrecorded",
    },
    {
        "action": "payments.payment_mode_set",
        "source": "internal",
        "critical": False,
        "resource": "payment_mode",
        "why": "selecting a tenant's payment model (conduit vs marketplace), or forcing one platform-wide, "
               "changes how its payments are routed; recording who set which mode keeps the routing "
               "decision attributable. Not critical: a down log must not strand the current selection",
    },
    {
        "action": "payments.submerchant_onboarded",
        "source": "internal",
        "critical": True,
        "resource": "submerchant",
        "why": "onboarding registers a tenant as a sub-merchant and sets its PAYOUT TARGET — where that "
               "shop's share of every split is sent. Silently creating or repointing it is a money-"
               "redirection move an incident review must attribute, so an unrecordable change must not "
               "proceed. The KYB data (which may carry PII) is never logged, only the action",
    },
    {
        "action": "payments.submerchant_destination_set",
        "source": "internal",
        "critical": True,
        "resource": "submerchant",
        "why": "setting a sub-merchant's destination id (hosted-onboarding callback or operator) decides "
               "where a shop's payouts land; like onboarding it is a money-path change that must be "
               "attributable and cannot proceed unrecorded",
    },
]
