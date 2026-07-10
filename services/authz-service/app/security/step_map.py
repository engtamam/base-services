# =============================================================================
# authz-service — Step->CWE map loader (stdlib tomllib, no extra dependency)
# =============================================================================
from __future__ import annotations

import os
import tomllib
from typing import Any

_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "step_cwe_map.toml")

# Built-in fallback so the guard works even with no file present.
DEFAULT_STEP_MAP: dict[str, dict[str, list[str]]] = {
    "db_query": {"cwe": ["CWE-89"], "checks": ["parameterized"]},
    "egress": {"cwe": ["CWE-918"], "checks": ["ssrf_allowlist"]},
    "file_write": {"cwe": ["CWE-22"], "checks": ["safe_object_key"]},
    "file_read": {"cwe": ["CWE-22"], "checks": ["safe_object_key"]},
    "respond": {"cwe": ["CWE-200", "CWE-532"], "checks": ["no_secret_leak"]},
}


def load_step_map(path: str = "") -> dict[str, dict[str, Any]]:
    """Load the step->CWE map. Falls back to the bundled file, then the default."""
    target = path or _DEFAULT_PATH
    try:
        with open(target, "rb") as fh:
            data = tomllib.load(fh)
        steps = data.get("steps")
        if isinstance(steps, dict) and steps:
            return steps
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return DEFAULT_STEP_MAP
