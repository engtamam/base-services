"""file-service — auditable actions.

Files ARE the customer's data, so the critical actions are the ones that let data leave or
disappear: minting a signed URL hands out access that outlives the request, and deleting is
irreversible. Uploads and scans are volume work and never block.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "file.uploaded",
        "source": "user",
        "critical": False,
    },
    {
        "action": "file.upload_started",
        "source": "user",
        "critical": False,
    },
    {
        "action": "file.upload_completed",
        "source": "user",
        "critical": False,
    },
    {
        "action": "file.upload_aborted",
        "source": "user",
        "critical": False,
    },
    {
        "action": "file.scanned",
        "source": "system",
        "critical": False,
    },
    {
        "action": "file.signed_url_issued",
        "source": "user",
        "critical": True,
        "why": "a signed URL grants access to a file to anyone holding the link, for as long "
               "as it lives; it is the platform's most direct data-exfiltration path and the "
               "record is the only way to know what was handed out",
    },
    {
        "action": "file.subjects_linked",
        "source": "system",
        "critical": True,
        "why": "this is the record that a named person appears in a file, and it is what makes "
               "their data reachable by an erasure — so it decides whether a future DSAR finds "
               "the file or reports absence. A wrong or missing link produces a receipt claiming "
               "data was erased that still exists, which is worse than an error because it is "
               "filed as evidence a right was honoured",
    },
    {
        "action": "file.deleted",
        "source": "user",
        "critical": True,
        "why": "deletion is irreversible and the file is gone, so nothing but this record can "
               "show who removed a customer's data",
    },
    {
        "action": "file.cross_tenant_downloaded",
        "source": "user",
        "critical": True,
        "why": "a platform operator reading another tenant's file BYTES across the tenant boundary "
               "is a privileged access that must leave a trace; the normal GET path was unaudited",
    },
    {
        "action": "file.cross_tenant_signed_url",
        "source": "user",
        "critical": True,
        "why": "a platform operator minting a signed URL for another tenant's file hands out an "
               "exfiltration link across the tenant boundary; it must be recorded like the download",
    },
    {
        "action": "file.inspected",
        "source": "admin",
        "critical": False,
        "why": "a platform operator browsed or inspected a specific tenant's files across the tenant "
               "boundary; who looked at whose files, and when, is what a security review asks about. "
               "Not critical: it is a read, and refusing it during an audit outage protects nothing",
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
]
