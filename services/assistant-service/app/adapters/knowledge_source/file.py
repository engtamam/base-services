"""File KnowledgeSource — a JSON KB on disk, the path set by an operator.

This is the ordinary case: an adopter writes a KB file, mounts it, points the service at it. The
path comes from configuration and never from a request, so no caller can redirect the assistant's
knowledge at a file of their choosing.

RELOAD IS BY CONTENT, NOT BY CLOCK. Each `load()` reads the file and hashes it. Same bytes, same
generation, and the indexer skips the rebuild; different bytes and it rebuilds. An mtime check
would rebuild on every `touch` and miss an edit that restored an old timestamp — the second is the
one that bites, because the KB then serves content the file no longer contains.

A DIRECTORY IS ALSO A FILE SOURCE. Pointed at a directory, it reads every `*.json` inside, sorted,
and merges them. That is what makes per-surface or per-team authoring practical: one file per area,
no merge conflicts on a single giant document. A duplicate id ACROSS files is an error and not a
last-one-wins, because two teams silently overwriting each other's help is exactly the failure this
layout is meant to prevent.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from app.core.kb import KbError, parse_kb
from app.ports.errors import ConfigError
from app.ports.knowledge_source import KbSnapshot


class FileKnowledgeSource:
    driver = "file"

    def __init__(self, path: str) -> None:
        if not path:
            raise ConfigError("KB_FILE_PATH is required for the file knowledge source")
        self._path = pathlib.Path(path)

    def _files(self) -> list[pathlib.Path]:
        if self._path.is_dir():
            return sorted(p for p in self._path.glob("*.json") if p.is_file())
        return [self._path]

    async def load(self) -> KbSnapshot:
        files = self._files()
        if not files or not any(p.exists() for p in files):
            # Loud, not empty. An assistant that answers "I don't know" to everything looks like a
            # working feature and can survive a long time before anyone checks the mount.
            raise ConfigError(
                f"knowledge base not found at {self._path} — the assistant would answer nothing "
                f"while appearing healthy, so this refuses instead")

        entries, seen, h = [], {}, hashlib.sha256()
        for p in files:
            try:
                raw_text = p.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot read knowledge base {p}: {exc}") from exc
            h.update(raw_text.encode("utf-8"))
            try:
                doc = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise KbError(f"{p.name}: not valid JSON ({exc})") from exc
            for entry in parse_kb(doc, source=self.driver):
                if entry.id in seen:
                    raise KbError(
                        f"duplicate entry id {entry.id!r} in {p.name} and {seen[entry.id]} — "
                        f"one would silently win and the other author would never know")
                seen[entry.id] = p.name
                entries.append(entry)

        return KbSnapshot(entries=tuple(entries), generation=h.hexdigest()[:32], driver=self.driver)

    async def health(self) -> bool:
        return any(p.exists() for p in self._files())

    async def close(self) -> None:
        return None
