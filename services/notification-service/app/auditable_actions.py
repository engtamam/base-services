"""notification-service — auditable actions.

Sending is the volume path and is never blocked — refusing to send because the audit log is down
would turn a logging problem into undelivered password resets. Preference changes are critical for
a narrower reason: they control whether a person can be contacted at all, and silently switching
someone's notifications off is a way to hide activity from them.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "notification.sent",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "notification.read",
        "source": "user",
        "critical": False,
    },
    {
        "action": "notification.drained",
        "source": "system",
        "critical": False,
    },
    {
        "action": "notification.deliveries_viewed",
        "source": "admin",
        "critical": False,
        "why": "an operator read the cross-tenant delivery log (recipients masked); who inspected "
               "delivery outcomes and when is worth the trail. Not critical: it is a read",
    },
    {
        "action": "whatsapp_template.registered",
        "source": "admin",
        "critical": False,
        "resource": "wa_template",
    },
    {
        "action": "whatsapp_template.submitted",
        "source": "admin",
        "critical": False,
        "resource": "wa_template",
    },
    {
        # A WABA-scoped Flow provisioned (created/cloned + published) at connect time. Not critical —
        # a Flow that failed to provision is retried, and refusing because the log blinked helps no
        # one — but worth a durable row: it names whether this call created the Flow or found it, and
        # its published state, so a shop whose Flow silently never went live is provable after the fact.
        "action": "whatsapp_flow.provisioned",
        "source": "admin",
        "critical": False,
        "resource": "wa_flow",
    },
    {
        # The Flow data-endpoint encryption key ensured for a tenant: a keypair generated + the private
        # half sealed (once), and the public half registered with Meta for a phone number. Critical —
        # it establishes the key that decrypts every dynamic Flow request; the row names whether a NEW
        # key was created (vs. re-registered) and which number, so a rotation or a first setup is
        # provable, and a surprise "created" on a tenant that already had one is a red flag.
        "action": "whatsapp_flow.encryption_ensured",
        "source": "admin",
        "critical": True,
        "resource": "wa_flow_encryption",
        "why": "Establishes/re-registers the RSA key that decrypts EVERY dynamic Flow request for a "
               "tenant. Without a durable row, a silent key change (or a first setup on a tenant that "
               "already had one — a possible takeover of the encrypted channel) leaves no trail; who "
               "set which number's key, and when, must be provable, so the request is refused if it "
               "cannot be recorded.",
    },
    {
        # The coexistence retention reaper ran: rows past their window were deleted so a shop's stored
        # conversations never live forever. Not critical — a purge that could not be recorded simply
        # runs again next cycle, and blocking it would only KEEP data longer, the opposite of the goal —
        # but the row names how many were removed and the retention period, so "retention is actually
        # running" (not silently stalled) is provable.
        "action": "whatsapp_coexistence.purged",
        "source": "admin",
        "critical": False,
        "resource": "wa_coexistence",
    },
    {
        # A media file uploaded to Meta (Resumable Upload) to get a carousel template's media handle.
        # Not critical — a failed upload is retried — but recorded: it names the type + size so a
        # template that shipped the wrong asset, or a burst of uploads, is provable after the fact.
        "action": "whatsapp_media.uploaded",
        "source": "admin",
        "critical": False,
        "resource": "wa_media",
    },
    {
        # An admin-triggered manual WhatsApp send (the diagnostic test-send). A privileged manual
        # send from a customer's number is exactly what a review asks about — who sent what, from
        # where — so it is recorded (last-4 of the recipient only, never the full number/body).
        "action": "whatsapp.test_send",
        "source": "admin",
        "critical": False,
        "resource": "whatsapp",
    },
    {
        "action": "preferences.updated",
        "source": "user",
        "critical": True,
        "why": "preferences decide whether a person can be contacted; silently disabling "
               "someone's alerts is how account activity gets hidden from its owner",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
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
    {
        "action": "operator.whatsapp_onboarded",
        "source": "admin",
        "critical": True,
        "resource": "waba",
        "why": "onboarding a business exchanges Meta's code for a token that can send in that "
               "shop's name and stores it as the tenant's credential — a credential-granting act, "
               "and like every other one here it must be attributable and is gone the moment it "
               "happens; the row names the WABA, never the code or the token",
    },
    {
        "action": "whatsapp.number_registered",
        "source": "internal",
        "critical": False,
        "resource": "phone",
        "why": "registering a phone number is what makes it able to SEND — the step between a stored "
               "token and a working channel. The row names the phone id and the outcome (registered, "
               "or a distinct reason) so a launch that silently never registered is provable after "
               "the fact. Not critical: refusing to register because the log blinked helps no one.",
    },
    {
        "action": "whatsapp.number_bind_conflict",
        "source": "internal",
        "critical": False,
        "resource": "phone",
        "why": "a second tenant tried to bind a phone number that ALREADY routes to another tenant. "
               "The routing map is first-wins (the incumbent is kept, never silently overwritten), so "
               "this is the trace that a collision happened at all — without it a duplicate/mistaken "
               "onboarding would be invisible. Names the phone id and the attempting tenant; the "
               "incumbent is not disclosed here. Not critical: the incumbent is already protected.",
    }

]
