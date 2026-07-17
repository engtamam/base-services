#!/usr/bin/env python
"""Gate: the admin panel is a master key. Its declarations must be true, and safe.

Each service declares its admin actions in `app/admin_actions.py`; the platform aggregates them into
the operator panel. The rules (R1-R6) live in `_actions_gate.py`, shared with safety:user-actions —
the two panels differ only in which manifest key they read.
"""
from __future__ import annotations

import sys

from _actions_gate import GateSpec, run

SPEC = GateSpec(
    gate_name="safety:admin",
    noun="admin actions",
    surface="panel",
    literal_name="ADMIN_ACTIONS",
    decl_filename="admin_actions.py",
    manifest_field="admin_actions",
    root_env="ADMIN_GATE_ROOT",
    tail="The admin panel is a master key. A declaration that lies about a route or a capability "
         "makes it misrepresent what an operator may do.",
)


if __name__ == "__main__":
    sys.exit(run(SPEC))
