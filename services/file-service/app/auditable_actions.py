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
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
