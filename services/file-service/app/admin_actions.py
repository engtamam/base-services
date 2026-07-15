"""file-service — admin actions: deliberately NONE.

Every route here is `_SELF` self-service: upload / list / download / delete operate ONLY on the
caller's own tenant, taken from the token and never from a parameter (see list_files). The two
capabilities — files.upload / files.read — are ordinary user grants, not a `platform.*` operator
surface, and there is no cross-tenant "list all files" or "manage storage" route to expose.

So the panel shows file-service with an empty action set. This file exists to make that ABSENCE a
reviewed decision rather than a forgotten one: if a genuine operator route is ever added (e.g. a
storage-quota or global-purge endpoint under a platform.files capability), declare it here.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = []
