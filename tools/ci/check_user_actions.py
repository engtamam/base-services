#!/usr/bin/env python
"""Gate: the end-user app catalog must be true, and safe.

Each service declares its self-service actions in `app/user_actions.py`; the platform aggregates them
into the end-user app (GET /api/v1/app/catalog). Same rules as safety:admin (R1-R6, in
`_actions_gate.py`) — the app reads `user_actions` instead of `admin_actions`. A lying declaration
would render a control that 404s or gate on a cap the route never checks; a scheme/host path is an
SSRF/redirect. The app is a map, never the authority: each route re-checks the capability AND
re-scopes to the caller's own tenant/identity.
"""
from __future__ import annotations

import sys

from _actions_gate import GateSpec, run

SPEC = GateSpec(
    gate_name="safety:user-actions",
    noun="user actions",
    surface="app",
    literal_name="USER_ACTIONS",
    decl_filename="user_actions.py",
    manifest_field="user_actions",
    root_env="USER_ACTIONS_GATE_ROOT",
    tail="The end-user app must not misrepresent what a user may do; a phantom route or a "
         "cap the route never checks is a lie the renderer would show.",
)


if __name__ == "__main__":
    sys.exit(run(SPEC))
