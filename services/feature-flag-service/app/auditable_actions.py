"""feature-flag-service — auditable actions.

A feature flag decides what code runs in production, and a config value decides how it behaves.
Changing either is a production change without a deployment, which is exactly why it needs a
record: 'nothing was deployed' is the first thing said in every incident where a flag was the
cause. Evaluation is the hot path and is never audited.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "flag.created",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "flag.updated",
        "source": "admin",
        "critical": True,
        "resource": "flag",
        "why": "a flag UPDATE includes a kill-switch flip or a rollout change — the operation most likely "
               "to be disputed after an incident; who flipped it and when must be provable, so unlike the "
               "initial create it is recorded critically",
    },
    {
        "action": "segment.created",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "segment.deleted",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "flag.deleted",
        "source": "admin",
        "critical": True,
        "why": "deleting a flag changes production behaviour with no deployment; without a "
               "record an incident review cannot tell what changed or when",
    },
    {
        "action": "flag.override_set",
        "source": "admin",
        "critical": True,
        "why": "an override targets specific subjects — it is how someone is quietly given or "
               "denied a feature, and an unattributable one cannot be reviewed",
    },
    {
        "action": "flag.override_removed",
        "source": "admin",
        "critical": True,
        "why": "removing an override changes who sees what, silently and without a deployment",
    },
    {
        "action": "config.updated",
        "source": "admin",
        "critical": True,
        "why": "a config value changes production behaviour instantly; 'nothing was deployed' "
               "is said in every incident where this was the cause",
    },
    {
        "action": "config.rolled_back",
        "source": "admin",
        "critical": True,
        "why": "a rollback rewrites live configuration during an incident, which is precisely "
               "when the timeline matters most",
    },
    {
        "action": "maintenance.toggled",
        "source": "admin",
        "critical": True,
        "why": "maintenance mode takes the platform off the air for every tenant",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
