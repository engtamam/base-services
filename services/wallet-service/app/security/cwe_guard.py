# =============================================================================
# wallet-service — CweGuard: pre/post-step self-check engine
# -----------------------------------------------------------------------------
# Every risky step runs its mapped CWE checks BEFORE (preconditions) and AFTER
# (postconditions). A violated precondition FAILS CLOSED (raises CweViolation),
# is logged with the exact CWE id + name from the bundled catalog, and is mapped
# to a safe generic error at the API boundary.
#
#     async with guard.step("egress", url=target) as step:
#         resp = await client.get(target)      # only runs if pre-checks passed
#         step.result = resp                    # post-checks run on clean exit
# =============================================================================
from __future__ import annotations

from typing import Any

from app.observability.logging import get_logger
from app.security.catalog import CweCatalog

_logger = get_logger("app.cwe_guard")


class CweViolation(Exception):
    def __init__(self, cwe_id: str, check: str, message: str) -> None:
        super().__init__(f"{cwe_id} [{check}]: {message}")
        self.cwe_id = cwe_id
        self.check = check
        self.message = message


class GuardCheck:
    """Base check. Subclasses set `name` + `cwe` and override pre/post."""

    name: str = ""
    cwe: str = ""

    def pre(self, ctx: dict[str, Any]) -> None:  # noqa: D401
        return None

    def post(self, ctx: dict[str, Any], result: Any) -> None:
        return None

    def _violate(self, message: str) -> CweViolation:
        return CweViolation(self.cwe, self.name, message)


class _Step:
    def __init__(self, guard: "CweGuard", step_type: str, ctx: dict[str, Any]) -> None:
        self._guard = guard
        self._step_type = step_type
        self.ctx = ctx
        self.result: Any = None

    async def __aenter__(self) -> "_Step":
        self._guard.run_pre(self._step_type, self.ctx)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._guard.run_post(self._step_type, self.ctx, self.result)
        return False  # never swallow


class CweGuard:
    def __init__(
        self,
        catalog: CweCatalog,
        step_map: dict[str, dict[str, Any]],
        checks: dict[str, GuardCheck],
        enabled: bool = True,
    ) -> None:
        self._catalog = catalog
        self._step_map = step_map
        self._checks = checks
        self._enabled = enabled

    def _checks_for(self, step_type: str) -> list[str]:
        return list(self._step_map.get(step_type, {}).get("checks", []))

    def step(self, step_type: str, **ctx: Any) -> _Step:
        return _Step(self, step_type, ctx)

    def run_pre(self, step_type: str, ctx: dict[str, Any]) -> None:
        if not self._enabled:
            return
        for name in self._checks_for(step_type):
            check = self._checks.get(name)
            if check is None:
                continue
            try:
                check.pre(ctx)
            except CweViolation as v:
                self._log(v, step_type)
                raise

    def run_post(self, step_type: str, ctx: dict[str, Any], result: Any) -> None:
        if not self._enabled:
            return
        for name in self._checks_for(step_type):
            check = self._checks.get(name)
            if check is None:
                continue
            try:
                check.post(ctx, result)
            except CweViolation as v:
                self._log(v, step_type)
                raise

    def _log(self, v: CweViolation, step_type: str) -> None:
        _logger.warning(
            "cwe_violation",
            extra={
                "step": step_type,
                "cwe": v.cwe_id,
                "cwe_name": self._catalog.name(v.cwe_id),
                "check": v.check,
            },
        )

    def report(self) -> list[dict[str, Any]]:
        """Coverage report: which CWEs this service guards, per step (for audit)."""
        out: list[dict[str, Any]] = []
        for step_type, spec in self._step_map.items():
            resolved = []
            for cwe_id in spec.get("cwe", []):
                try:
                    resolved.append(self._catalog.resolve(cwe_id))
                except KeyError:
                    resolved.append({"id": cwe_id, "name": cwe_id, "type": "unknown"})
            out.append({"step": step_type, "checks": spec.get("checks", []), "cwe": resolved})
        return out
