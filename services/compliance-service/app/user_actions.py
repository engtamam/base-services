"""compliance-service — end-user (self-service) actions: deliberately NONE.

Compliance is operated by a DPO / compliance officer, not self-service by the data subject. Every
route is `_ADMIN` (platform.compliance / compliance.manage) or `_READ` (…/compliance.read): the
officer RECORDS a consent grant/withdrawal, OPENS and processes a DSAR, places legal holds, and reads
the registers on the subject's behalf. There is no `_SELF` / `require_user` route a data subject calls
directly, and no `compliance.self` capability — so the end-user app shows nothing here.

This whole surface is the OPERATOR's (see admin_actions). This file exists to make the ABSENCE a
reviewed decision, not a forgotten one: if a genuine data-subject self-service route is ever added
(e.g. a `POST /api/v1/compliance/me/dsar` under a `compliance.self` capability), declare it here.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = []
