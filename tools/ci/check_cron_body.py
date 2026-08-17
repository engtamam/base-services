#!/usr/bin/env python3
"""safety:cron-body — a cron-driven route must accept the empty POST its own scheduler sends.

The platform's scheduled-job template POSTs an EMPTY body with a JSON content type. A route that
declares a REQUIRED Pydantic body (`body: Model`) then makes FastAPI answer 422 before the handler
runs — so the job never executes, and the failure lives only in a Job's exit code while every probe
and dashboard stays green. This is exactly how the nightly BACKUP never ran (v0.16.16), and it had
already happened once before to campaigns' tick. This gate makes it impossible a third time.

For every path a cronJob calls (deploy/services/*.values.yaml), the matching POST route in that
service's internal.py must EITHER declare no body param OR declare it optional (`body: Model | None =
None`, or any default). A required body on a cron route fails the build.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
_INTERNAL_PREFIX = "/internal"


def _cron_paths() -> list[tuple[str, str]]:
    """(service, full path) for every cronJob declared in the deploy values."""
    out: list[tuple[str, str]] = []
    for f in sorted((ROOT / "deploy" / "services").glob("*.values.yaml")):
        service = f.stem.replace(".values", "")
        for m in re.finditer(r'path:\s*["\']?(/internal/[^\s"\']+)', f.read_text(encoding="utf-8")):
            out.append((service, m.group(1)))
    return out


def _route_params(text: str, deco_path: str) -> str | None:
    """The parameter list of the POST handler whose decorator names `deco_path`, or None if there is
    no such POST route (a GET cron, or a path not served here — neither has a body to validate)."""
    dm = re.search(r'@router\.post\(\s*["\']' + re.escape(deco_path) + r'["\']', text)
    if not dm:
        return None
    start = text.find("async def", dm.end())
    if start < 0:
        return None
    op = text.find("(", start)
    depth, i = 0, op
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return text[op + 1:i]


def _has_required_body(params: str) -> bool:
    """A `body:` annotation with NO default is required — FastAPI 422s an empty POST against it."""
    m = re.search(r'\bbody\s*:\s*[^,=)]+?(=\s*[^,)]+)?(,|$)', params, re.S)
    return bool(m) and not m.group(1)


def main() -> int:
    offenders: list[str] = []
    checked = 0
    for service, full in _cron_paths():
        internal = ROOT / "services" / service / "app" / "api" / "internal.py"
        if not internal.exists():
            continue
        params = _route_params(internal.read_text(encoding="utf-8"), full[len(_INTERNAL_PREFIX):])
        if params is None:
            continue  # a GET cron, or served elsewhere — no body to validate
        checked += 1
        if _has_required_body(params):
            offenders.append(f"{service}: POST {full} declares a REQUIRED body — its cron POSTs "
                             f"empty, so FastAPI 422s it and the job never runs")
    if offenders:
        print("safety:cron-body FAILED — a cron route rejects its own scheduler's empty POST:")
        for o in offenders:
            print(f"  - {o}")
        print("  Fix: make the body optional — `body: Model | None = None` then `body = body or Model()`.")
        return 1
    print(f"      {checked} cron POST route(s) accept the empty POST their scheduler sends")
    return 0


if __name__ == "__main__":
    sys.exit(main())
