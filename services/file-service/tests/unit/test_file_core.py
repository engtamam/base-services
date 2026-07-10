"""Unit tests: upload/download, validation, dedup+refcount, signed URLs."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.object_store.memory import MemoryObjectStore
from app.adapters.scanner.magic import MagicScanner
from app.adapters.scanner.none import NoneScanner
from app.adapters.signed_url.hmac_signer import HmacSigner
from app.core.file_repository import FileRepository
from app.core.file_service import FileService, TooLarge, TypeNotAllowed
from app.ports.errors import NotFound
from app.ports.scanner import Rejected

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


def run(c):
    return asyncio.run(c)


def _svc(scanner=None, allowed=frozenset(), max_bytes=1000, ttl=300):
    obj = MemoryObjectStore()
    svc = FileService(repo=FileRepository(MemoryDataStore()), object_store=obj,
                      scanner=scanner or NoneScanner(), signed_url=HmacSigner("s" * 32, ttl),
                      max_upload_bytes=max_bytes, allowed_content_types=allowed)
    return svc, obj


def _up(svc, data=PNG, ct="image/png", tenant="t1", name="a.png"):
    return run(svc.upload(tenant=tenant, owner="o", filename=name, content_type=ct, data=data))


def test_upload_download_roundtrip():
    svc, _ = _svc()
    f = _up(svc)
    assert f.size == len(PNG) and len(f.sha256) == 64
    _, data = run(svc.download(f.id))
    assert data == PNG


def test_too_large_rejected():
    svc, _ = _svc(max_bytes=4)
    with pytest.raises(TooLarge):
        _up(svc, data=b"12345")


def test_type_not_allowed():
    svc, _ = _svc(allowed=frozenset({"image/png"}))
    with pytest.raises(TypeNotAllowed):
        _up(svc, data=b"hi", ct="text/plain")


def test_magic_blocks_executable():
    svc, _ = _svc(scanner=MagicScanner(True))
    with pytest.raises(Rejected):
        _up(svc, data=b"MZ\x90\x00exe", ct="image/png")


def test_dedup_and_refcount():
    svc, obj = _svc()
    f1 = _up(svc, name="one.png")
    f2 = _up(svc, name="two.png")           # identical bytes
    assert f1.sha256 == f2.sha256 and f1.id != f2.id
    r1 = run(svc.delete(f1.id))
    assert r1["blob_removed"] is False       # still referenced by f2
    assert run(svc.download(f2.id))[1] == PNG
    r2 = run(svc.delete(f2.id))
    assert r2["blob_removed"] is True        # last reference gone
    with pytest.raises(NotFound):
        run(obj.get(f1.sha256))


def test_signed_url_valid_and_tampered():
    svc, _ = _svc()
    f = _up(svc)
    token = svc.signed_token(f.id)
    assert run(svc.resolve_signed(token))[1] == PNG
    assert run(svc.resolve_signed(token[:-3] + "aaa")) is None


def test_signed_url_expired():
    svc, _ = _svc(ttl=-10)                    # already expired
    f = _up(svc)
    assert run(svc.resolve_signed(svc.signed_token(f.id))) is None
