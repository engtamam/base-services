"""stream-service — auditable actions.

Recordings are video of real people, so this service handles biometric data by construction.
Deleting a recording destroys evidence; unlocking one REMOVES the legal hold that was protecting
it, which is the subtler and more dangerous of the two. Sessions, PTZ moves and probes are
operational and never block.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "source.created",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "source.deleted",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "source.probed",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "triggers.updated",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "session.started",
        "source": "user",
        "critical": False,
    },
    {
        "action": "session.stopped",
        "source": "user",
        "critical": False,
    },
    {
        "action": "ptz.moved",
        "source": "user",
        "critical": False,
    },
    {
        "action": "discovery.run",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "recording.locked",
        "source": "admin",
        "critical": True,
        "why": "locking places a legal hold on footage; if we cannot show when it began we "
               "cannot defend having kept it",
    },
    {
        "action": "recording.unlocked",
        "source": "admin",
        "critical": True,
        "why": "unlocking REMOVES the legal hold and re-enables deletion of protected footage "
               "— an unattributable unlock is how held evidence quietly becomes deletable",
    },
    {
        "action": "recording.deleted",
        "source": "admin",
        "critical": True,
        "why": "deleting footage destroys biometric data and any evidence it held, "
               "irreversibly",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
