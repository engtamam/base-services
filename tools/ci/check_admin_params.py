#!/usr/bin/env python3
"""safety:admin-params — a declared admin action can actually be performed.

The admin catalog is what a console renders a form from. `safety:admin` already checks that an
action's route and capability EXIST. That is not enough: a route can exist, the capability can be
real, and the action can still be impossible to perform, because the form the console builds does
not match the body the API accepts.

Three shapes of that, all found in billing-service, all of which passed every existing gate:

  1. `billing.create_plan` declared NO params at all. The API requires `name` and a non-empty
     `prices`. A console renders an empty form, posts an empty body, and the operator gets a 422
     they cannot fix — the panel never told them there was anything to fill in.

  2. `billing.update_plan` declared `prices`, `limits` and `trial_days` as `"string"`. The API
     wants an array, an object and an integer. A console renders three text boxes and every submit
     is a 422. This one was introduced by the same change that added the route, which is exactly
     when nobody is looking at the console.

  3. The contract had no `array` type at all, so a list-valued field could not be described
     truthfully even by someone trying. authz-service had been calling a list of capabilities an
     `"object"` for the same reason.

WHAT IS CHECKED, against the GENERATED OpenAPI spec — not a hand-written list, so it cannot drift
from the running service:

  * the action's (path, method) exists in the spec
  * every REQUIRED body field of that operation is declared as a param
  * every declared param's type is compatible with the spec's type for that field
  * a declared param that the operation does not accept is reported — it is a form field that
    goes nowhere

WHAT IT DOES NOT DO. It does not check help text, ordering or labels; those are taste. And it only
inspects `application/json` bodies plus query and path parameters, because that is what these
services expose.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = pathlib.Path(os.environ.get("SERVICES_ROOT") or (ROOT / "services"))
SPECS = pathlib.Path(os.environ.get("OPENAPI_DIR") or (ROOT / "docs" / "openapi"))

# How a declared param type maps onto a JSON Schema type. `enum` is a string the console renders
# as a picker; `number` accepts an integer, because every integer is a valid number.
_COMPATIBLE = {
    "string": {"string"},
    "enum": {"string"},
    "integer": {"integer"},
    "number": {"number", "integer"},
    "boolean": {"boolean"},
    "object": {"object"},
    "array": {"array"},
}


def _actions(service: pathlib.Path) -> list[dict]:
    path = service / "app" / "admin_actions.py"
    if not path.is_file():
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target == "ADMIN_ACTIONS" and value is not None:
            try:
                return ast.literal_eval(value)
            except ValueError:
                return []
    return []


def _spec(service_name: str) -> dict | None:
    path = SPECS / f"{service_name}.openapi.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _resolve(spec: dict, node: dict) -> dict:
    """Follow a $ref one level, which is all FastAPI emits for a request body."""
    ref = node.get("$ref")
    if not ref or not ref.startswith("#/"):
        return node
    cur: object = spec
    for part in ref[2:].split("/"):
        if not isinstance(cur, dict) or part not in cur:
            return {}
        cur = cur[part]
    return cur if isinstance(cur, dict) else {}


def _body_schema(spec: dict, op: dict) -> dict:
    content = ((op.get("requestBody") or {}).get("content") or {})
    schema = (content.get("application/json") or {}).get("schema") or {}
    return _resolve(spec, schema)


def _types_of(node: dict) -> set[str]:
    """The JSON types a field accepts, flattening the anyOf FastAPI emits for optionals."""
    out = set()
    if "type" in node:
        out.add(node["type"])
    for alt in node.get("anyOf", []) or node.get("oneOf", []):
        if isinstance(alt, dict) and alt.get("type") and alt["type"] != "null":
            out.add(alt["type"])
    return out


def _normalise(path: str) -> str:
    """`/plans/{plan_id}` and `/plans/{id}` describe the same route to a console."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def main() -> int:
    problems: list[str] = []
    checked = 0

    for service in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        if service.name.startswith("_"):
            continue
        actions = _actions(service)
        if not actions:
            continue
        spec = _spec(service.name)
        if spec is None:
            problems.append(f"{service.name}: declares admin actions but has no generated OpenAPI "
                            f"spec to check them against")
            continue
        routes = {(_normalise(p), m.lower()): op
                  for p, item in (spec.get("paths") or {}).items()
                  for m, op in item.items() if isinstance(op, dict)}

        for action in actions:
            checked += 1
            aid = action.get("id", "?")
            key = (_normalise(action.get("path", "")), str(action.get("method", "")).lower())
            op = routes.get(key)
            if op is None:
                problems.append(f"{service.name}:{aid}: {action.get('method')} "
                                f"{action.get('path')} is not a route this service serves")
                continue

            declared = {p.get("name"): p for p in (action.get("params") or [])}
            placeholders = set(re.findall(r"\{([^}]+)\}", action.get("path", "")))
            body = _body_schema(spec, op)
            props = body.get("properties") or {}
            required = set(body.get("required") or [])

            for ph in sorted(placeholders - {n for n, p in declared.items()
                                             if p.get("in") == "path"}):
                problems.append(
                    f"{service.name}:{aid}: path contains {{{ph}}} and no param declares it — a "
                    f"console has nothing to substitute and calls the URL literally")

            for field in sorted(required - set(declared)):
                problems.append(
                    f"{service.name}:{aid}: body field {field!r} is REQUIRED by the API and is not "
                    f"declared — a console builds a form without it and every submit is a 422")

            query = {p.get("name"): p for p in (op.get("parameters") or [])
                     if p.get("in") == "query"}
            # A path param is checked against the ACTION'S OWN path template, never the API's
            # parameter name. `/invoices/{id}/resubmit` with a param named `id` is internally
            # consistent and produces the right URL, even though the route calls it `invoice_id`
            # — a console substitutes into the action's path and never sees the API's spelling.
            # The first version of this gate compared the two names and reported twenty-five
            # pieces of correct code, which is how a gate stops being read.
            placeholders = set(re.findall(r"\{([^}]+)\}", action.get("path", "")))
            for name, param in declared.items():
                want = param.get("type", "string")
                where = param.get("in", "body")
                if where == "body":
                    schema = props.get(name)
                    if schema is None:
                        problems.append(f"{service.name}:{aid}: declares body field {name!r}, "
                                        f"which the API does not accept — the form field goes nowhere")
                        continue
                    actual = _types_of(_resolve(spec, schema))
                elif where == "path":
                    if name not in placeholders:
                        problems.append(
                            f"{service.name}:{aid}: declares path param {name!r}, but the "
                            f"action's own path {action.get('path')!r} has no {{{name}}} to "
                            f"substitute it into")
                    continue
                else:
                    schema = query.get(name)
                    if schema is None:
                        problems.append(f"{service.name}:{aid}: declares {where} param {name!r}, "
                                        f"which this route does not take")
                        continue
                    actual = _types_of(_resolve(spec, schema.get("schema") or {}))
                if not actual:
                    continue          # the spec did not say; nothing to contradict
                if not (_COMPATIBLE.get(want, {want}) & actual):
                    problems.append(
                        f"{service.name}:{aid}: {name!r} is declared {want!r} but the API wants "
                        f"{'/'.join(sorted(actual))} — a console renders the wrong control and "
                        f"every submit is a 422")

    if problems:
        print("      declared admin actions that cannot actually be performed:")
        for p in problems:
            print(f"        {p}")
        print("      A panel that renders a form the API rejects is worse than no button.")
        return 1

    print(f"      {checked} admin action(s) match the API they call")
    return 0


if __name__ == "__main__":
    sys.exit(main())
