"""What backup-service must capture to restore file-service, and what it may skip.

A pure declaration, read as a literal. The `safety:backup-coverage` gate checks it against the
collections the code actually writes, so a collection added later without a line here fails the build.

The durable metadata is backed up: the file records (id-keyed) and the subject→file links that make
a DSAR erasure COMPLETE (keyed "{tenant}|{subject_ref}|{file_id}", declared as a template — losing
them would leave a person's files unfindable for erasure). The FILE CONTENT is content-addressed blob
bytes in the object store; those are backed up by the object store's own lifecycle, not sealed into
this archive.

Two collections are excluded. upload_sessions are transient multipart uploads that a sweeper aborts;
a session mid-flight at backup time is meaningless to restore. blob_refs is the per-blob reference
count (sha256 -> {count}); it is written via incr (invisible to the coverage gate) and is DERIVED —
exactly the number of files that reference each blob. It is rebuilt from `files` on import, so a
restore cannot leave a live blob with a zero count that a later delete would wrongly GC.
"""
from __future__ import annotations

from typing import Any

BACKUP: dict[str, Any] = {
    "object_prefix": "",   # blob bytes are content-addressed in the object store — its own backup story
    "collections": [
        {"name": "files", "tenant_scoped": True},
        {"name": "file_subjects", "tenant_scoped": True, "key": "{tenant}|{subject_ref}|{file_id}"},
    ],
    "excluded": [
        {"name": "upload_sessions",
         "reason": "transient multipart upload state (id-keyed); a sweeper aborts stale ones and a "
                   "session mid-flight at backup time cannot be resumed after a restore"},
        {"name": "blob_refs",
         "reason": "per-blob reference count (sha256 -> {count}), written via incr. DERIVED — it is "
                   "the number of files pointing at each blob — and rebuilt from `files` on import "
                   "(see internal.py), so a restored blob keeps the count that guards its GC"},
    ],
}
