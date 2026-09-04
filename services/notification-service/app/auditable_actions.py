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
    }

]
