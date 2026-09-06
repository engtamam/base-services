"""assistant-service — auditable actions: what this service must be able to PROVE it did.

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
        "subject.erased",
        actor=subject_ref,               # a PSEUDONYM — see below
        resource=f"subject:{subject_ref}",
        tenant=principal.tenant,
        ip=request.client.host,
        metadata={"category": "conversations"},
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
        # Not critical: refusing to ANSWER a help question because the audit log is unreachable
        # would take the product down to protect a record of who asked what. The record is useful
        # — it shows which questions the KB cannot answer — but it is not worth the availability.
        "action": "assistant.abstained",
        "source": "user",
        "critical": False,
        "resource": "question",
        "why": "the questions the assistant could NOT answer are the highest-value signal for "
               "what the knowledge base is missing, and they are invisible anywhere else",
    },
    {
        "action": "assistant.kb_reloaded",
        "source": "admin",
        "critical": True,
        "resource": "knowledge_base",
        "why": "a reload changes what the product asserts to every user of the tenant; an "
               "unprovable one leaves an investigation unable to place a wrong answer in time, "
               "and the previous knowledge base is already gone",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "resource": "subject",
        "why": "erasure is irreversible and the data itself is gone afterwards, so nothing else "
               "can show it happened; without a record we cannot prove we honoured the DSAR",
    },
    {
        "action": "assistant.retention_purged",
        "source": "internal",
        "critical": True,
        "resource": "conversations",
        "why": "the retention sweep bulk-deletes conversations past their window; once they are "
               "gone this line is the only proof a purge happened, when, and how many — the same "
               "irreversible-deletion reasoning as subject.erased. Counts only, never content",
    },
    {
        "action": "assistant.message_attributed",
        "source": "internal",
        "critical": False,
        "resource": "conversation",
        "why": "a conversation turn's own tenant+subject_ref age out with the content under "
               "retention, so a legitimate legal/law-enforcement question arriving after a purge "
               "has nothing left to say a subject spoke with a tenant at a time; this pseudonymous "
               "line (no content) preserves that attribution independently of content retention. "
               "Not critical: refusing to ANSWER because the log is unreachable is not worth it",
    },
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
]
