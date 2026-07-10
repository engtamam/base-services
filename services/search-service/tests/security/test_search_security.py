"""Security: deny-by-default, internal token, no injection, tenant isolation, fail-fast."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


def _make_app() -> TestClient:
    from app.config import get_settings

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="i" * 40, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET="h" * 40, RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def test_deny_by_default(client):
    assert client.post("/api/v1/search", json={"index": "a", "text": "x"}).status_code == 401
    assert client.get("/api/v1/search/suggest?index=a&prefix=x").status_code == 401


def test_internal_index_requires_token(client):
    assert client.post("/internal/search/index",
                       json={"index": "a", "id": "d", "tenant": "t", "fields": {}}).status_code == 401


def test_no_raw_query_passthrough():
    # the query layer refuses operator characters — there is no backend expression to inject
    from app.core.query import QueryError, validate_text
    for payload in ('title:secret OR tenant:*', 'a AND b~', 'x^10', '*:*'):
        with pytest.raises(QueryError):
            validate_text(payload, max_len=512)


def test_filters_reject_operator_objects():
    from app.core.query import QueryError, normalize_filters
    with pytest.raises(QueryError):
        normalize_filters({"price": {"$gt": 0}})       # no mongo/es operator injection


def test_unknown_driver_fails_fast():
    from app.config import Settings
    from app.container import build_container
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError):
        build_container(Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40,
                                 SEARCH_INDEX_DRIVER="grep"))
