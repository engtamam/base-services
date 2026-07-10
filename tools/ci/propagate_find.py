#!/usr/bin/env python
"""Propagate the `find()` primitive into every service's copy of the DataStore.

Seventeen services, seventeen copies of the port. That is the design — self-contained means no
shared runtime library — but it does NOT mean the copies may disagree about what a DataStore is.
A shared fix has to land everywhere.

**Additive, never a copy.** The copies have legitimately diverged: billing carries
`incr_if_below` (quota), wallet carries `cas` (money). Overwriting one service's port with
another's silently deletes those. This script inserts `find` / `scan_all` and leaves everything
else alone, and refuses to run if it would drop a method.

Idempotent: running it twice changes nothing.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
SOURCE = SERVICES / "file-service"

PORT_FIND = '''    async def find(
        self,
        collection: str,
        where: dict[str, Any],
        *,
        limit: int = 100,
        after: str | None = None,
        sort: tuple[str, str] = ("created", "desc"),
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Rows matching `where`, ordered, paged. Returns (rows, next_cursor).

        `where` is EQUALITY ONLY: `{field: value}`, or `{field: [v1, v2]}` for membership. It is
        not a query language: there are no operators, so there is nothing to inject.

        `where` must be non-empty. `find(collection, {})` would be a cross-tenant leak one typo
        away from a repository that meant to scope by tenant.

        `after` is an opaque KEYSET cursor, never an offset. An offset is O(offset) and unstable:
        a row inserted between two pages makes the second page silently skip one.

        `sort` is (field, "asc"|"desc"), made total by an `id` tie-break.
        """
        ...

    async def scan_all(self, collection: str, limit: int) -> list[dict[str, Any]]:
        """EVERY row in a collection. A sweeper or a migration — never a request path."""
        ...

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        """DEPRECATED alias of `scan_all`. Use `find`."""
        ...
'''

PORT_OLD_LIST = (
    "    async def list(self, collection: str, limit: int = 100)"
    " -> list[dict[str, Any]]: ...\n"
)

MEMORY_FIND = '''    @staticmethod
    def _matches(row: dict[str, Any], where: dict[str, Any]) -> bool:
        """Equality only. `{f: v}` or `{f: [v1, v2]}`. No operators => nothing to inject."""
        for field, expected in where.items():
            actual = row.get(field)
            if isinstance(expected, (list, tuple, set)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    async def find(
        self,
        collection: str,
        where: dict[str, Any],
        *,
        limit: int = 100,
        after: str | None = None,
        sort: tuple[str, str] = ("created", "desc"),
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not where:
            raise ConfigError("find() requires a non-empty `where` - use scan_all() to read all")
        field, direction = sort
        if direction not in SORT_DIRECTIONS:
            raise ConfigError(f"sort direction must be one of {SORT_DIRECTIONS}: {direction!r}")
        limit = max(1, int(limit))

        rows = [r for r in self._data.get(collection, {}).values() if self._matches(r, where)]
        descending = direction == "desc"
        rows.sort(key=lambda r: (_cursor._key(r.get(field)), str(r.get("id", ""))),
                  reverse=descending)

        position = _cursor.decode(after) if after else None
        if position is not None:
            rows = [r for r in rows
                    if _cursor.after(r.get(field), str(r.get("id", "")), position, descending)]

        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = _cursor.encode(last.get(field), str(last.get("id", "")))
        return copy.deepcopy(page), next_cursor

    async def scan_all(self, collection: str, limit: int) -> list[dict[str, Any]]:
        """Every row. Sweepers and migrations only - see the port."""
        rows = list(self._data.get(collection, {}).values())
        return copy.deepcopy(rows[: max(0, int(limit))])

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        return await self.scan_all(collection, limit)
'''

MEMORY_OLD_LIST = '''    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = list(self._data.get(collection, {}).values())
        return copy.deepcopy(rows[: max(0, limit)])
'''

PG_OLD_LIST = '''    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        pool = await self._ensure()
        async with pool.acquire() as con:
            rows = await con.fetch(
                f"SELECT value FROM {_TABLE} WHERE collection=$1 LIMIT $2",  # nosec B608 - _TABLE is a trusted constant; values parameterized
                collection, max(0, limit),
            )
        return [json.loads(r["value"]) for r in rows]
'''


def _pg_find() -> str:
    """Lift the verified implementation straight from file-service, so there is one source."""
    src = (SOURCE / "app/adapters/datastore/postgres.py").read_text(encoding="utf-8")
    start = src.index("    async def find(")
    end = src.index("    async def health(")
    return src[start:end]


def _pg_indexes() -> str:
    src = (SOURCE / "app/adapters/datastore/postgres.py").read_text(encoding="utf-8")
    start = src.index("# A `find` without an index")
    end = src.index("class PostgresDataStore")
    return src[start:end]


def _methods(text: str) -> set[str]:
    return set(re.findall(r"^\s*async def (\w+)", text, re.MULTILINE))


def services() -> list[pathlib.Path]:
    return sorted(p for p in SERVICES.iterdir() if p.is_dir() and (p / "app" / "ports").is_dir())


def patch(path: pathlib.Path, transform) -> str:
    before = path.read_text(encoding="utf-8")
    after = transform(before)
    if after is None:
        return "already"
    lost = _methods(before) - _methods(after)
    if lost:
        raise SystemExit(f"REFUSING: {path} would lose methods {sorted(lost)}")
    path.write_text(after, encoding="utf-8")
    return "patched"


def do_cursor(svc: pathlib.Path) -> str:
    # NOT app/core/cursor.py: search-service already owns that name for its RANKING cursor
    # (score, doc_id). Overwriting it silently broke search's pagination. The datastore cursor
    # lives with the adapters that use it, where no service logic can collide with it.
    target = svc / "app" / "adapters" / "datastore" / "cursor.py"
    source = (SOURCE / "app/adapters/datastore/cursor.py").read_text(encoding="utf-8")
    if target.exists() and target.read_text(encoding="utf-8") == source:
        return "already"
    target.write_text(source, encoding="utf-8")
    return "patched"


def do_port(svc: pathlib.Path) -> str:
    def t(text: str) -> str | None:
        if "async def find(" in text:
            return None
        if PORT_OLD_LIST not in text:
            raise SystemExit(f"REFUSING: unexpected port shape in {svc.name}")
        text = text.replace(PORT_OLD_LIST, PORT_FIND)
        if "SORT_DIRECTIONS" not in text:
            text = text.replace(
                "from typing import Any, Protocol, runtime_checkable\n",
                "from typing import Any, Protocol, runtime_checkable\n\n"
                "# Ordering directions a caller may ask for. Anything else is a typo.\n"
                'SORT_DIRECTIONS = ("asc", "desc")\n')
        return text
    return patch(svc / "app/ports/datastore.py", t)


def do_memory(svc: pathlib.Path) -> str:
    def t(text: str) -> str | None:
        if "async def find(" in text:
            return None
        if MEMORY_OLD_LIST not in text:
            raise SystemExit(f"REFUSING: unexpected memory adapter shape in {svc.name}")
        text = text.replace(MEMORY_OLD_LIST, MEMORY_FIND)
        if "from app.adapters.datastore import cursor as _cursor" not in text:
            text = text.replace(
                "import copy\nfrom typing import Any\n",
                "import copy\nfrom typing import Any\n\n"
                "from app.adapters.datastore import cursor as _cursor\n"
                "from app.ports.datastore import SORT_DIRECTIONS\n"
                "from app.ports.errors import ConfigError\n")
        return text
    return patch(svc / "app/adapters/datastore/memory.py", t)


def do_postgres(svc: pathlib.Path) -> str:
    def t(text: str) -> str | None:
        if "async def find(" in text:
            return None
        if PG_OLD_LIST not in text:
            raise SystemExit(f"REFUSING: unexpected postgres adapter shape in {svc.name}")
        text = text.replace(PG_OLD_LIST, _pg_find())
        if "from app.adapters.datastore import cursor as _cursor" not in text:
            text = text.replace(
                "from app.adapters.lazy import load\n",
                "from app.adapters.lazy import load\n"
                "from app.adapters.datastore import cursor as _cursor\n"
                "from app.ports.datastore import SORT_DIRECTIONS\n")
        if "_INDEXES" not in text:
            text = text.replace('_TABLE = "kv_store"\n\n', f'_TABLE = "kv_store"\n\n{_pg_indexes()}')
            text = text.replace(
                '                    "value jsonb NOT NULL, PRIMARY KEY (collection, key))"\n'
                "                )\n",
                '                    "value jsonb NOT NULL, PRIMARY KEY (collection, key))"\n'
                "                )\n"
                "                for statement in _INDEXES:\n"
                "                    await con.execute(statement)\n")
        return text
    return patch(svc / "app/adapters/datastore/postgres.py", t)


def main() -> int:
    for svc in services():
        results = {
            "cursor": do_cursor(svc),
            "port": do_port(svc),
            "memory": do_memory(svc),
            "postgres": do_postgres(svc),
        }
        state = "already up to date" if set(results.values()) == {"already"} else \
            ", ".join(f"{k}={v}" for k, v in results.items())
        print(f"  {svc.name:24} {state}")
    print(f"\n{len(services())} services carry find().")
    return 0


if __name__ == "__main__":
    sys.exit(main())
