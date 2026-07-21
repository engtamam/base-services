"""connector-service — auditable actions.

Connections carry CREDENTIALS to a customer's own systems, so connecting and disconnecting are the
critical pair: a connection nobody authorised is an exfiltration path into a third-party CRM, and
a silent disconnect breaks a data flow the customer is relying on. Sync runs are high volume and
recorded without blocking.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "connector.spec_registered",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "connector.sync_started",
        "source": "user",
        "critical": False,
    },
    {
        "action": "connector.sync_run",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "connector.connected",
        "source": "admin",
        "critical": True,
        "why": "connecting stores credentials to a customer's own CRM or ERP; an "
               "unattributable connection is an exfiltration path into someone else's system",
    },
    {
        "action": "connector.disconnected",
        "source": "admin",
        "critical": True,
        "why": "disconnecting silently stops a data flow the customer depends on, and destroys "
               "the stored credential — the customer will ask who did it",
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
    }

]
