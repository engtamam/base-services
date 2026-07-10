"""TemplateRenderer port — render a template with variables (injection-safe)."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TemplateRenderer(Protocol):
    driver: str

    def render(self, subject_tmpl: str, body_tmpl: str, variables: dict[str, Any]) -> tuple[str, str]:
        """Return (subject, body). Substitution only — never evaluates code (no SSTI)."""
        ...
