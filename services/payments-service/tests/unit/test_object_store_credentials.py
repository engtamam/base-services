"""Object-store credential resolution — the four provider modes and the acceptance criteria.

The s3 extra (aioboto3/botocore/aiohttp) is not installed in the unit venv, and it does not need to be:
build_session takes the aioboto3 module as an ARGUMENT (so a stub proves mode selection and every
startup refusal), and the async fetchers import aiohttp/botocore lazily (so injected fakes prove the
Alibaba dialect mapping — the part most likely to be wrong). The live provider wiring is exercised
separately when the extra is present.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.adapters.object_store import credentials as C
from app.ports.errors import ConfigError


class _FakeSession:
    def __init__(self, **kw):
        self.kwargs = kw                       # records botocore_session= when the metadata modes pass it


class _FakeAioboto3:
    def __init__(self):
        self.built: list[dict] = []

    def Session(self, **kw):
        self.built.append(kw)
        return _FakeSession(**kw)


def run(c):
    return asyncio.run(c)


# ── build_session: mode selection + every startup refusal (criteria 3,4,5) ──────────────────────
def test_static_with_both_keys_passes_them_to_the_client():
    a = _FakeAioboto3()
    sess, kw = C.build_session(a, C.CredentialConfig(mode="static", access_key="AK", secret_key="SK"))
    assert kw == {"aws_access_key_id": "AK", "aws_secret_access_key": "SK"}
    assert isinstance(sess, _FakeSession) and "botocore_session" not in sess.kwargs


def test_static_is_the_default_mode():
    a = _FakeAioboto3()
    _, kw = C.build_session(a, C.CredentialConfig(access_key="AK", secret_key="SK"))
    assert kw["aws_access_key_id"] == "AK"


def test_half_a_static_credential_is_refused_at_startup():
    # criterion 5 — kept exactly from the old adapter.
    for cfg in (C.CredentialConfig(mode="static", access_key="AK"),
                C.CredentialConfig(mode="static", secret_key="SK")):
        with pytest.raises(ConfigError, match="half a credential"):
            C.build_session(_FakeAioboto3(), cfg)


def test_empty_static_credentials_fall_back_to_the_ambient_chain():
    _, kw = C.build_session(_FakeAioboto3(), C.CredentialConfig(mode="static"))
    assert kw == {}                            # ambient chain, unchanged behaviour


def test_imds_is_the_ambient_chain_untouched():
    _, kw = C.build_session(_FakeAioboto3(), C.CredentialConfig(mode="imds"))
    assert kw == {}


def test_an_unknown_mode_is_refused_never_silently_static():
    # criterion 4 — the CronJob-timeZone lesson: reject the unknown value where a human is standing.
    with pytest.raises(ConfigError, match="is not one of"):
        C.build_session(_FakeAioboto3(), C.CredentialConfig(mode="gcp-metadata"))


def test_metadata_modes_require_an_endpoint_at_startup():
    # criterion 3 — a missing endpoint fails at boot, not at the first nightly backup. This refusal
    # happens BEFORE any botocore import, so it runs without the s3 extra.
    for mode in ("alibaba-ecs-ram", "sts-endpoint"):
        with pytest.raises(ConfigError, match="OBJECTSTORE_METADATA_ENDPOINT"):
            C.build_session(_FakeAioboto3(), C.CredentialConfig(mode=mode))


def test_sts_endpoint_requires_its_field_mapping():
    cfg = C.CredentialConfig(mode="sts-endpoint", endpoint="http://sts.local",
                             sts_fields={"access_key": "", "secret_key": "SK"})
    with pytest.raises(ConfigError, match="field name for 'access_key'"):
        C.build_session(_FakeAioboto3(), cfg)


# ── field mapping: the Alibaba dialect onto botocore's keys ─────────────────────────────────────
def _install_fake_botocore(monkeypatch):
    exc = types.ModuleType("botocore.exceptions")

    class CredentialRetrievalError(Exception):
        def __init__(self, provider="", error_msg=""):
            super().__init__(f"{provider}: {error_msg}")

    exc.CredentialRetrievalError = CredentialRetrievalError
    boto = types.ModuleType("botocore")
    boto.exceptions = exc
    monkeypatch.setitem(sys.modules, "botocore", boto)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exc)
    return CredentialRetrievalError


def test_map_fields_maps_alibaba_names_onto_botocore_keys():
    data = {"AccessKeyId": "STS.abc", "AccessKeySecret": "sk", "SecurityToken": "tok",
            "Expiration": "2099-01-01T00:00:00Z", "Code": "Success"}
    out = C._map_fields(data, C._ALIBABA_FIELDS, where="x")
    assert out == {"access_key": "STS.abc", "secret_key": "sk", "token": "tok",
                   "expiry_time": "2099-01-01T00:00:00Z"}


def test_map_fields_refuses_a_missing_mandatory_field(monkeypatch):
    err = _install_fake_botocore(monkeypatch)
    with pytest.raises(err, match="no secret_key"):
        C._map_fields({"AccessKeyId": "a"}, C._ALIBABA_FIELDS, where="x")


def test_map_fields_allows_absent_token_and_expiry():
    out = C._map_fields({"AccessKeyId": "a", "AccessKeySecret": "b"}, C._ALIBABA_FIELDS, where="x")
    assert out["token"] is None and out["expiry_time"] is None


# ── the Alibaba metadata fetch: token, role discovery, Code check ────────────────────────────────
class _Resp:
    def __init__(self, status, text="", json_data=None):
        self.status, self._text, self._json = status, text, json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._text

    async def json(self, content_type=None):
        return self._json


class _HttpSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _r(self, method, url, headers):
        self.calls.append((method, url, dict(headers or {})))
        return self.routes.get((method, url)) or self.routes[(method, "*")]

    def get(self, url, headers=None):
        return self._r("GET", url, headers)

    def put(self, url, headers=None):
        return self._r("PUT", url, headers)


def _install_fake_aiohttp(monkeypatch, session):
    mod = types.ModuleType("aiohttp")
    mod.ClientError = type("ClientError", (Exception,), {})
    mod.ClientTimeout = lambda total=None: types.SimpleNamespace(total=total)
    mod.ClientSession = lambda timeout=None: session
    monkeypatch.setitem(sys.modules, "aiohttp", mod)


def test_alibaba_fetch_discovers_the_role_sends_the_token_and_maps_creds(monkeypatch):
    _install_fake_botocore(monkeypatch)
    base = "http://metadata.local"
    creds = {"AccessKeyId": "STS.k", "AccessKeySecret": "s", "SecurityToken": "t",
             "Expiration": "2099-01-01T00:00:00Z", "Code": "Success"}
    routes = {
        ("PUT", base + C._ALIBABA_TOKEN_PATH): _Resp(200, text="TOK123"),
        ("GET", base + C._ALIBABA_ROLE_PATH): _Resp(200, text="my-backup-role\n"),
        ("GET", base + C._ALIBABA_ROLE_PATH + "my-backup-role"): _Resp(200, json_data=creds),
    }
    sess = _HttpSession(routes)
    _install_fake_aiohttp(monkeypatch, sess)
    cfg = C.CredentialConfig(mode="alibaba-ecs-ram", endpoint=base)     # role auto-discovered
    out = run(C._fetch_alibaba(cfg))
    assert out == {"access_key": "STS.k", "secret_key": "s", "token": "t",
                   "expiry_time": "2099-01-01T00:00:00Z"}
    # the IMDSv2-style token was obtained and then presented on the credential GET
    cred_get = [c for c in sess.calls if c[0] == "GET" and c[1].endswith("my-backup-role")][0]
    assert cred_get[2].get("X-aliyun-ecs-metadata-token") == "TOK123"


def test_alibaba_fetch_raises_a_named_error_when_code_is_not_success(monkeypatch):
    err = _install_fake_botocore(monkeypatch)
    base = "http://metadata.local"
    routes = {
        ("PUT", base + C._ALIBABA_TOKEN_PATH): _Resp(404),
        ("GET", base + C._ALIBABA_ROLE_PATH + "r"): _Resp(200, json_data={"Code": "Failed"}),
    }
    _install_fake_aiohttp(monkeypatch, _HttpSession(routes))
    cfg = C.CredentialConfig(mode="alibaba-ecs-ram", endpoint=base, role="r")
    with pytest.raises(err, match="Code="):
        run(C._fetch_alibaba(cfg))


def test_sts_fetch_maps_configurable_field_names(monkeypatch):
    _install_fake_botocore(monkeypatch)
    url = "http://sts.local/creds"
    routes = {("GET", url): _Resp(200, json_data={"ak": "A", "sk": "B", "tk": "T", "exp": "E"})}
    _install_fake_aiohttp(monkeypatch, _HttpSession(routes))
    cfg = C.CredentialConfig(mode="sts-endpoint", endpoint=url,
                             sts_fields={"access_key": "ak", "secret_key": "sk",
                                         "token": "tk", "expiry_time": "exp"})
    out = run(C._fetch_sts(cfg))
    assert out == {"access_key": "A", "secret_key": "B", "token": "T", "expiry_time": "E"}
