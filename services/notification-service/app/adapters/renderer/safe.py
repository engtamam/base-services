"""Safe template renderer — stdlib string.Template ($var only, no code/logic).

Substitution-only means there is NO expression evaluation, so a malicious template
or variable value can never execute code (defeats SSTI, CWE-94/1336). A missing
variable raises rather than silently leaking the raw placeholder.
"""
from __future__ import annotations

from string import Template
from typing import Any

from app.ports.errors import PortError


def _render_one(tmpl: str, variables: dict[str, Any]) -> str:
    try:
        # substitute (not safe_substitute) → missing key raises KeyError.
        return Template(tmpl).substitute(variables)
    except (KeyError, ValueError) as exc:
        raise PortError(f"template render error: {exc}") from exc


class SafeRenderer:
    driver = "safe"

    def render(self, subject_tmpl: str, body_tmpl: str, variables: dict[str, Any]) -> tuple[str, str]:
        # Coerce values to str; never interpret them as templates (no recursion).
        vars_str = {k: str(v) for k, v in variables.items()}
        return _render_one(subject_tmpl, vars_str), _render_one(body_tmpl, vars_str)
