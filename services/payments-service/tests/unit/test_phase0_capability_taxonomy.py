"""Phase 0 — the extensibility seams, with ZERO behaviour change, tested adversarially.

Proves the capability/model/onboarding taxonomy, each adapter's declaration, the registry metadata,
the FAIL-CLOSED capability assertion in create_link, the CredentialResolver Protocol, and the
centralised event topics. Written from an attacker's seat: a malformed, hostile, or stringly-typed
declaration must only ever make the core refuse MORE, never permit more; a refused create must leave
no stuck claim and no persisted request; the topic catalogue and taxonomy sets must be immutable.
None of this changes the conduit's behaviour (the rest of the suite proves that).
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.event_bus.memory import MemoryEventBus
from app.adapters.payment import SELECTABLE_DRIVERS, build_gateway, describe_drivers
from app.adapters.payment import _REGISTRY
from app.adapters.payment.console import ConsoleGateway
from app.adapters.payment.null import NullGateway
from app.adapters.payment.tuwaiqpay import TuwaiqPayGateway
from app.core import payment_events
from app.core.merchant_credentials import (DRIVER, WEBHOOK_SECRET, CredentialResolver,
                                           MerchantGateways, merchant_secret_store)
from app.core.payment_requests import PaymentRequestStore
from app.core.payments import PaymentNotConfigured, PaymentService, TOPIC_COMPLETED, TOPIC_FAILED
from app.ports import payment_gateway as pg

KEK = "k" * 40


def run(c):
    return asyncio.run(c)


# ═══════════════════════════════════════ taxonomy ═══════════════════════════════
def test_models_set_is_exact():
    assert pg.MODELS == {pg.MODEL_CONDUIT, pg.MODEL_MARKETPLACE}


def test_capabilities_set_is_exact():
    assert pg.CAPABILITIES == {pg.CAP_CHECKOUT, pg.CAP_ONBOARDING, pg.CAP_SPLIT, pg.CAP_PAYOUT}


def test_onboarding_modes_set_is_exact():
    assert pg.ONBOARDING_MODES == {pg.ONBOARD_API, pg.ONBOARD_HOSTED}


def test_model_string_values_are_the_wire_contract():
    assert pg.MODEL_CONDUIT == "conduit" and pg.MODEL_MARKETPLACE == "marketplace"


def test_capability_string_values_are_the_wire_contract():
    assert (pg.CAP_CHECKOUT, pg.CAP_ONBOARDING, pg.CAP_SPLIT, pg.CAP_PAYOUT) == (
        "checkout", "onboarding", "split", "payout")


def test_onboarding_string_values_are_the_wire_contract():
    assert pg.ONBOARD_API == "api" and pg.ONBOARD_HOSTED == "hosted"


@pytest.mark.parametrize("s", ["MODELS", "CAPABILITIES", "ONBOARDING_MODES"])
def test_taxonomy_sets_are_immutable_frozensets(s):
    val = getattr(pg, s)
    assert isinstance(val, frozenset)
    with pytest.raises(AttributeError):
        val.add("evil")                                  # cannot inject a value at runtime


def test_the_three_namespaces_do_not_collide():
    # A value must mean one thing: a model is never mistaken for a capability, etc.
    assert pg.MODELS.isdisjoint(pg.CAPABILITIES)
    assert pg.CAPABILITIES.isdisjoint(pg.ONBOARDING_MODES)
    assert pg.MODELS.isdisjoint(pg.ONBOARDING_MODES)


# ═══════════════════════════════ adapter declarations ═══════════════════════════
@pytest.mark.parametrize("cls", [TuwaiqPayGateway, ConsoleGateway])
def test_conduit_adapters_declare_checkout_only(cls):
    assert cls.model == pg.MODEL_CONDUIT
    assert cls.capabilities == {pg.CAP_CHECKOUT}
    assert cls.onboarding_modes == frozenset()


def test_null_adapter_declares_nothing():
    assert NullGateway.model == ""
    assert NullGateway.capabilities == frozenset()
    assert NullGateway.onboarding_modes == frozenset()


@pytest.mark.parametrize("driver", SELECTABLE_DRIVERS)
def test_every_selectable_adapter_declares_a_known_model(driver):
    assert _REGISTRY[driver].model in pg.MODELS


@pytest.mark.parametrize("driver", SELECTABLE_DRIVERS)
def test_every_selectable_adapter_declares_only_known_capabilities(driver):
    caps = pg.capabilities_of(_REGISTRY[driver])
    assert caps and caps <= pg.CAPABILITIES          # non-empty and no typo/unknown capability


@pytest.mark.parametrize("driver", SELECTABLE_DRIVERS)
def test_every_selectable_adapter_onboarding_subset(driver):
    assert pg.onboarding_modes_of(_REGISTRY[driver]) <= pg.ONBOARDING_MODES


@pytest.mark.parametrize("driver", SELECTABLE_DRIVERS)
def test_adapter_driver_attr_matches_registry_key(driver):
    assert _REGISTRY[driver].driver == driver


# ═══════════════════════ helpers: defensive / adversarial ═══════════════════════
def test_helpers_on_a_real_adapter():
    gw = build_gateway("console", {"link_base": "https://x.test"})
    assert pg.capabilities_of(gw) == {pg.CAP_CHECKOUT}
    assert pg.model_of(gw) == pg.MODEL_CONDUIT
    assert pg.onboarding_modes_of(gw) == frozenset()


def test_helpers_default_safely_on_a_bare_object():
    class _Bare:
        pass
    assert pg.capabilities_of(_Bare()) == frozenset()
    assert pg.model_of(_Bare()) == ""
    assert pg.onboarding_modes_of(_Bare()) == frozenset()


class _Decl:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_capabilities_none_is_empty():
    assert pg.capabilities_of(_Decl(capabilities=None)) == frozenset()


def test_capabilities_bare_string_is_one_cap_not_characters():
    # The classic bug: frozenset("checkout") would be {'c','h','e',...}. It must be {"checkout"}.
    assert pg.capabilities_of(_Decl(capabilities="checkout")) == {"checkout"}


def test_capabilities_list_becomes_a_set():
    assert pg.capabilities_of(_Decl(capabilities=["checkout", "split"])) == {"checkout", "split"}


def test_capabilities_non_iterable_fails_closed_to_empty():
    assert pg.capabilities_of(_Decl(capabilities=123)) == frozenset()


def test_capabilities_elements_coerced_to_str():
    assert pg.capabilities_of(_Decl(capabilities={1, 2})) == {"1", "2"}


def test_capabilities_helper_returns_a_frozenset():
    assert isinstance(pg.capabilities_of(_Decl(capabilities=["checkout"])), frozenset)


def test_mutating_the_result_cannot_corrupt_the_adapter():
    # The returned set is a fresh frozenset; even if a caller tries, the adapter is untouched.
    d = _Decl(capabilities={"checkout"})
    caps = pg.capabilities_of(d)
    with pytest.raises(AttributeError):
        caps.add("payout")
    assert pg.capabilities_of(d) == {"checkout"}      # adapter unchanged


def test_model_non_string_is_empty():
    assert pg.model_of(_Decl(model=123)) == ""
    assert pg.model_of(_Decl(model=None)) == ""


def test_onboarding_none_is_empty():
    assert pg.onboarding_modes_of(_Decl(onboarding_modes=None)) == frozenset()


def test_onboarding_bare_string_is_one_mode():
    assert pg.onboarding_modes_of(_Decl(onboarding_modes="hosted")) == {"hosted"}


def test_helper_never_invents_a_capability():
    # Across a spread of hostile inputs (none of which legitimately NAMES a capability), the result must
    # never contain a real capability — a malformed declaration can only ever make the core refuse more.
    for bad in [None, 0, "", [], (), {}, 3.14, object(), b"checkout"]:
        assert pg.capabilities_of(_Decl(capabilities=bad)).isdisjoint(pg.CAPABILITIES)


# ═══════════════════════════════ registry metadata ═════════════════════════════
def test_describe_lists_exactly_selectable_drivers():
    rows = {r["driver"] for r in describe_drivers()}
    assert rows == set(SELECTABLE_DRIVERS)


def test_describe_never_lists_the_null_gateway():
    assert "null" not in {r["driver"] for r in describe_drivers()}


def test_describe_tuwaiqpay_metadata():
    row = next(r for r in describe_drivers() if r["driver"] == "tuwaiqpay")
    assert row["model"] == pg.MODEL_CONDUIT
    assert row["capabilities"] == [pg.CAP_CHECKOUT]
    assert row["onboarding_modes"] == []


def test_describe_is_deterministic():
    assert describe_drivers() == describe_drivers()


def test_describe_capabilities_are_sorted_lists():
    for r in describe_drivers():
        assert r["capabilities"] == sorted(r["capabilities"])


def test_describe_rows_expose_only_safe_keys_no_secrets():
    # An enumeration surface must never carry a credential/hint/base — only the public menu fields.
    allowed = {"driver", "model", "capabilities", "onboarding_modes"}
    for r in describe_drivers():
        assert set(r) == allowed


def test_describe_needs_no_credentials_to_enumerate():
    # Reads class metadata, not an instance — so it cannot leak or require per-tenant secrets.
    assert describe_drivers()        # does not raise despite no secret store in scope


# ═══════════════════════════ CredentialResolver Protocol ════════════════════════
def test_merchant_gateways_satisfies_the_resolver_protocol():
    resolver = MerchantGateways(merchant_secret_store(MemoryDataStore(), KEK))
    assert isinstance(resolver, CredentialResolver)


def test_object_missing_a_method_is_not_a_resolver():
    class _Partial:
        async def gateway_for(self, tenant): ...
        async def driver_for(self, tenant): ...
        # no configured()
    assert not isinstance(_Partial(), CredentialResolver)


def test_empty_object_is_not_a_resolver():
    assert not isinstance(object(), CredentialResolver)


def test_a_full_stub_is_a_resolver():
    class _Full:
        async def gateway_for(self, tenant): ...
        async def driver_for(self, tenant): ...
        async def configured(self, tenant): ...
    assert isinstance(_Full(), CredentialResolver)


# ═══════════════ capability assertion in create_link (attacker view) ════════════
class _Gw:
    """A stand-in gateway with attacker-chosen metadata; prepare_checkout must NEVER be reached when
    the capability is missing (so it records if it was called)."""
    driver = "fake"
    model = pg.MODEL_MARKETPLACE

    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.onboarding_modes = frozenset()
        self.checkout_called = False

    def prepare_checkout(self, **kw):
        self.checkout_called = True
        raise AssertionError("prepare_checkout must not be called without the checkout capability")


class _Resolver:
    def __init__(self, gw):
        self._gw = gw

    async def gateway_for(self, tenant):
        return self._gw

    async def driver_for(self, tenant):
        return getattr(self._gw, "driver", "")

    async def configured(self, tenant):
        return True


def _svc(resolver, store=None):
    store = store or MemoryDataStore()
    return PaymentService(
        requests=PaymentRequestStore(store), gateways=resolver, event_bus=MemoryEventBus(),
        redirect_base_url="https://conduit.example", allowed_hosts=frozenset({"x.test"}),
        link_ttl_seconds=3600), store


@pytest.mark.parametrize("caps", [
    frozenset(),                       # declares nothing
    None,                              # malformed
    frozenset({"onboarding", "split", "payout"}),   # marketplace caps but NO checkout
    frozenset({"CHECKOUT"}),           # wrong case — must be exact
    frozenset({"checkout "}),          # trailing space — must be exact
    frozenset({"check"}),              # prefix — must be exact
    "checkout_but_a_string_split_to_chars",         # stringly-typed junk
])
def test_create_link_refuses_without_exact_checkout_capability(caps):
    gw = _Gw(caps)
    svc, _ = _svc(_Resolver(gw))
    with pytest.raises(PaymentNotConfigured):
        run(svc.create_link(tenant="t1", reference="o1", amount_minor=1000, currency="SAR"))
    assert gw.checkout_called is False      # refused BEFORE any gateway call


def test_refused_create_persists_no_payment_request():
    svc, store = _svc(_Resolver(_Gw(frozenset())))
    with pytest.raises(PaymentNotConfigured):
        run(svc.create_link(tenant="t1", reference="o1", amount_minor=1000, currency="SAR"))
    # Nothing was recorded — no half-made order a later webhook could settle.
    assert run(PaymentRequestStore(store).active_by_reference(tenant="t1", reference="o1")) is None


def test_refused_create_releases_the_claim_so_a_retry_is_not_stuck():
    # A capability refusal must RELEASE the (tenant, reference) claim, or that order could never be
    # minted again even after the gateway is fixed — a denial-of-service on one order id.
    store = MemoryDataStore()
    bad, _ = _svc(_Resolver(_Gw(frozenset())), store)
    with pytest.raises(PaymentNotConfigured):
        run(bad.create_link(tenant="acme", reference="ord-1", amount_minor=1000, currency="SAR"))
    # Now a correctly-configured console gateway on the SAME store + reference must succeed.
    secrets = merchant_secret_store(store, KEK)
    run(secrets.set(tenant="acme", name=DRIVER, value="console"))
    run(secrets.set(tenant="acme", name=WEBHOOK_SECRET, value="s" * 20))
    good = PaymentService(
        requests=PaymentRequestStore(store),
        gateways=MerchantGateways(secrets, console_link_base="https://x.test/checkout",
                                  allow_console=True),
        event_bus=MemoryEventBus(), redirect_base_url="https://c.example",
        allowed_hosts=frozenset({"x.test"}), link_ttl_seconds=3600)
    link = run(good.create_link(tenant="acme", reference="ord-1", amount_minor=1000, currency="SAR"))
    assert link.request_id


def test_gateway_with_checkout_plus_extra_caps_still_works():
    # A marketplace adapter that ALSO offers plain checkout must not be refused.
    gw = _Gw(frozenset({"checkout", "split", "payout"}))
    gw.prepare_checkout = lambda **kw: _ok_session()   # type: ignore
    svc, _ = _svc(_Resolver(gw))
    link = run(svc.create_link(tenant="t1", reference="o9", amount_minor=500, currency="SAR"))
    assert link.request_id


def _ok_session():
    from app.ports.payment_gateway import CheckoutSession
    return CheckoutSession(ok=True, provider="fake", provider_ref="pr_1",
                           redirect_url="https://x.test/pay/pr_1")


def test_unconfigured_tenant_still_refuses_via_null_branch():
    # The pre-existing fail-closed path (no creds -> null gateway) is unchanged.
    svc, _ = _svc(MerchantGateways(merchant_secret_store(MemoryDataStore(), KEK)))
    with pytest.raises(PaymentNotConfigured):
        run(svc.create_link(tenant="ghost", reference="o1", amount_minor=1000, currency="SAR"))


# ═══════════════════════════════ centralised topics ════════════════════════════
def test_topic_values():
    assert payment_events.TOPIC_COMPLETED == "payment.completed"
    assert payment_events.TOPIC_FAILED == "payment.failed"


def test_payments_reexports_the_same_topics():
    assert TOPIC_COMPLETED == payment_events.TOPIC_COMPLETED
    assert TOPIC_FAILED == payment_events.TOPIC_FAILED


def test_emitted_catalogue_is_the_two_topics():
    assert set(payment_events.EMITTED) == {TOPIC_COMPLETED, TOPIC_FAILED}


def test_emitted_is_an_immutable_tuple():
    assert isinstance(payment_events.EMITTED, tuple)


def test_emitted_has_no_duplicates():
    assert len(payment_events.EMITTED) == len(set(payment_events.EMITTED))
