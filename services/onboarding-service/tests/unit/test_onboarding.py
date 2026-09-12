"""onboarding-service — the edge cases, because the happy path was never the risk.

Four failures matter here, in descending order of how expensive they are:

    a gift issued twice            money out, and farmable at scale
    a flow completing while a requirement is unmet   trust granted on a check that did not pass
    an orphan after partial failure a tenant nobody owns, needing manual cleanup
    a grant reported as delivered when it was not    a customer told they have credit they lack

Everything below is one of those four, or a case that leads to one.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.evaluator import Evaluator
from app.core.flows import FlowError, pick_flow, validate
from app.core.grants import GrantService
from app.core.saga import Saga, Step
from app.ports.entitlement import FAILED, GRANTED, Receipt
from app.ports.requirement_source import SATISFIED, UNKNOWN, UNSATISFIED, Answer

FLOW = {
    "flow": "b2b",
    "audience": {"account_type": "business"},
    "requirements": [
        {"id": "email", "satisfied_by": "identity.email_verified", "blocks": ["verified"]},
        {"id": "verified", "satisfied_by": "verification.business", "blocks": ["credit"],
         "scope": "tenant"},
        {"id": "credit", "satisfied_by": "local.granted", "grants": "welcome",
         "once_per": "verified_identity", "scope": "tenant"},
        {"id": "avatar", "satisfied_by": "identity.avatar", "optional": True},
    ],
}


def run(c):
    return asyncio.run(c)


class _Src:
    driver = "fake"

    def __init__(self, facts):
        self.facts = facts

    async def check(self, *, fact, tenant, subject):
        return Answer(status=self.facts.get(fact, UNSATISFIED))


class _Local:
    def __init__(self, granted=False):
        self.granted = granted

    async def check(self, *, fact, tenant, subject, req):
        return Answer(status=SATISFIED if self.granted else UNSATISFIED)


def _eval(identity=None, verification=None, granted=False, flow=None):
    ev = Evaluator(sources={"identity": _Src(identity or {}),
                            "verification": _Src(verification or {})},
                   local_facts=_Local(granted))
    return run(ev.evaluate(flow or FLOW, tenant="acme", subject="u1"))


# ── completion is derived, and cannot be faked ────────────────────────────────
def test_a_flow_is_not_complete_while_a_required_requirement_is_unmet():
    assert _eval().complete is False


def test_optional_requirements_never_block_completion():
    """Conflating optional with required is how a nudge to upload an avatar gates a paid product."""
    state = _eval(identity={"email_verified": SATISFIED},
                  verification={"business": SATISFIED}, granted=True)
    assert state.complete is True
    assert [r.id for r in state.requirements if r.optional and r.status != SATISFIED] == ["avatar"]


def test_an_unknown_requirement_never_counts_as_satisfied():
    """The safety property. An unreachable identity-service must not complete anyone's onboarding —
    least of all while the platform is already unhealthy and nobody is watching.

    Every requirement here resolves to UNKNOWN, with none merely `unsatisfied`. That isolation is
    the point: an earlier version of this test left one requirement unsatisfied, which kept
    `complete` False for the wrong reason and let a mutation turning unknown into satisfied pass
    unnoticed.
    """
    remote_only = {"flow": "remote", "audience": {}, "requirements": [
        {"id": "a", "satisfied_by": "identity.email_verified"},
        {"id": "b", "satisfied_by": "verification.business"},
    ]}
    ev = Evaluator(sources={}, local_facts=None)          # nothing configured at all
    state = run(ev.evaluate(remote_only, tenant="acme", subject="u1"))
    assert [r.status for r in state.requirements] == [UNKNOWN, UNKNOWN]
    assert state.complete is False
    assert all(not r.satisfied for r in state.requirements)


# ── regression: a satisfied requirement can become unsatisfied again ──────────
def test_an_expired_verification_reverts_the_flow_and_reblocks_what_it_gated():
    """A stored tick could never do this: it is written when a fact becomes true and nothing
    rewrites it when the fact stops being true."""
    good = _eval(identity={"email_verified": SATISFIED}, verification={"business": SATISFIED})
    assert [r for r in good.requirements if r.id == "credit"][0].blocked_by == []

    lapsed = _eval(identity={"email_verified": SATISFIED}, verification={})   # expired
    credit = [r for r in lapsed.requirements if r.id == "credit"][0]
    assert credit.blocked_by == ["verified"]
    assert lapsed.complete is False


# ── the invited user: nothing to skip ─────────────────────────────────────────
def test_an_invited_user_sees_only_what_is_theirs():
    """Not skip-logic — the tenant requirements simply are not in their flow."""
    invited = {"flow": "invited", "audience": {"joined_by": "invite"},
               "requirements": [{"id": "email", "satisfied_by": "identity.email_verified"}]}
    state = _eval(identity={"email_verified": SATISFIED}, flow=invited)
    assert state.complete is True
    assert [r.id for r in state.requirements] == ["email"]


def test_no_matching_flow_is_refused_rather_than_defaulted():
    """Guessing would ask a personal account for a commercial registration — or let a business
    skip verification and collect a gift."""
    assert pick_flow([FLOW], {"account_type": "personal"}) is None


# ── blocking reports WHAT blocks ──────────────────────────────────────────────
def test_a_blocked_requirement_names_its_blocker():
    state = _eval()
    blocked = {r.id: r.blocked_by for r in state.requirements if r.blocked_by}
    assert blocked == {"verified": ["email"], "credit": ["verified"]}


def test_next_offers_only_what_can_be_acted_on_now():
    state = _eval()
    assert [r.id for r in state.next_up] == ["email", "avatar"]     # required before optional


def test_the_response_has_no_step_index():
    """A count is honest about a set; 'step 3 of 7' implies an order that does not exist."""
    body = _eval().to_dict()
    assert "step" not in body and "progress" not in body and "percent" not in body
    assert "remaining" in body and "next" in body


# ── flow drift: declarations change while people are mid-flow ─────────────────
def test_a_requirement_added_later_reopens_a_completed_flow():
    """Someone who finished yesterday must not stay 'complete' when the platform adds a new
    requirement — completion is derived, so it simply recomputes."""
    before = _eval(identity={"email_verified": SATISFIED},
                   verification={"business": SATISFIED}, granted=True)
    assert before.complete is True

    grown = {**FLOW, "requirements": FLOW["requirements"] + [
        {"id": "tax_id", "satisfied_by": "verification.tax"}]}
    after = _eval(identity={"email_verified": SATISFIED},
                  verification={"business": SATISFIED}, granted=True, flow=grown)
    assert after.complete is False
    assert "tax_id" in after.to_dict()["remaining"]


def test_a_requirement_removed_later_does_not_strand_anyone():
    shrunk = {**FLOW, "requirements": [r for r in FLOW["requirements"] if r["id"] != "credit"]}
    state = _eval(identity={"email_verified": SATISFIED},
                  verification={"business": SATISFIED}, flow=shrunk)
    assert state.complete is True


# ── grants: the expensive failures ────────────────────────────────────────────
class _Ledger:
    def __init__(self):
        self.rows = {}

    async def find(self, key):
        return self.rows.get(key)

    async def record(self, key, **kw):
        self.rows[key] = {"reference": kw.get("reference", "")}


class _Ent:
    driver = "fake"

    def __init__(self, ok=True):
        self.ok, self.calls = ok, 0

    async def issue(self, req):
        self.calls += 1
        return (Receipt(status=GRANTED, reference="w-1") if self.ok
                else Receipt(status=FAILED, detail="wallet down"))


class _Ident:
    def __init__(self, ref):
        self.ref = ref

    async def claim_ref_for(self, tenant, subject):
        return self.ref


_SPECS = {"welcome": {"amount": "200000", "currency": "SAR"}}


def _grants(ent=None, ledger=None, ref="clm_abc"):
    return GrantService(entitlement=ent or _Ent(), ledger=ledger or _Ledger(),
                        identity_lookup=_Ident(ref), specs=_SPECS)


def test_the_same_verified_identity_cannot_collect_twice_from_a_new_tenant():
    """THE attack. Sign up, take the credit, delete the account, sign up again — the exact hole in
    both reference implementations, whose key was the tenant or the email."""
    ledger, ent = _Ledger(), _Ent()
    g = _grants(ent, ledger)
    first = run(g.issue(kind="welcome", once_per="verified_identity", tenant="t1", subject="u1"))
    second = run(g.issue(kind="welcome", once_per="verified_identity", tenant="t2", subject="u9"))
    assert first.status == GRANTED
    assert second.status == "already_granted"
    assert ent.calls == 1                    # the backend was never asked a second time


def test_concurrent_claims_grant_once():
    ledger, ent = _Ledger(), _Ent()
    g = _grants(ent, ledger)

    async def race():
        return await asyncio.gather(*[
            g.issue(kind="welcome", once_per="verified_identity", tenant="t1", subject="u1")
            for _ in range(5)])

    results = asyncio.run(race())
    assert sum(1 for r in results if r.status == GRANTED) <= 1
    assert ent.calls <= 1


def test_an_unverified_identity_is_refused_not_granted():
    """Otherwise the strongest control in the design depends on the customer choosing to verify."""
    out = run(_grants(ref="").issue(kind="welcome", once_per="verified_identity",
                                    tenant="t1", subject="u1"))
    assert out.status == "refused" and not out.granted


def test_a_failed_grant_is_not_recorded_so_the_retry_can_still_succeed():
    """Recording the failure would permanently block the retry that would have worked, leaving the
    customer owed a gift the system believed it had given."""
    ledger = _Ledger()
    failed = run(_grants(_Ent(ok=False), ledger).issue(
        kind="welcome", once_per="verified_identity", tenant="t1", subject="u1"))
    assert failed.status == FAILED and ledger.rows == {}

    retried = run(_grants(_Ent(ok=True), ledger).issue(
        kind="welcome", once_per="verified_identity", tenant="t1", subject="u1"))
    assert retried.status == GRANTED


def test_a_grant_the_platform_never_defined_is_refused():
    g = GrantService(entitlement=_Ent(), ledger=_Ledger(), identity_lookup=_Ident("c"), specs={})
    assert run(g.issue(kind="welcome", once_per="verified_identity",
                       tenant="t", subject="u")).status == "refused"


def test_an_entitlement_adapter_that_raises_does_not_look_like_a_delivered_grant():
    class _Boom:
        driver = "boom"

        async def issue(self, req):
            raise RuntimeError("adapter bug")

    out = run(_grants(_Boom()).issue(kind="welcome", once_per="verified_identity",
                                     tenant="t", subject="u"))
    assert out.granted is False


# ── compensation ──────────────────────────────────────────────────────────────
def test_a_later_failure_leaves_no_orphan():
    order = []

    def step(name, fail=False):
        async def do():
            if fail:
                raise RuntimeError(f"{name} failed")
            order.append(f"do:{name}")
            return name

        async def undo(res):
            order.append(f"undo:{name}")

        return Step(name=name, do=do, undo=undo)

    out = run(Saga([step("provision_tenant"), step("register_owner", fail=True)]).run())
    assert out.ok is False
    assert out.compensated == ["provision_tenant"]
    assert order == ["do:provision_tenant", "undo:provision_tenant"]


def test_compensation_runs_in_reverse():
    order = []

    def step(name, fail=False):
        async def do():
            if fail:
                raise RuntimeError("boom")
            return name

        async def undo(res):
            order.append(name)

        return Step(name=name, do=do, undo=undo)

    run(Saga([step("a"), step("b"), step("c"), step("d", fail=True)]).run())
    assert order == ["c", "b", "a"]        # newest first


def test_something_irreversible_is_recorded_not_silently_skipped():
    async def do():
        return "sent"

    async def fail():
        raise RuntimeError("boom")

    out = run(Saga([
        Step(name="send_email", do=do, undo=None,
             irreversible_reason="an email cannot be unsent"),
        Step(name="later", do=fail),
    ]).run())
    # residue now carries the created resource REFERENCE so an operator can locate the orphan (r2-08)
    assert out.residue == [{"step": "send_email", "why": "an email cannot be unsent",
                            "reference": "sent"}]


def test_a_failing_undo_produces_a_resumable_work_list():
    async def do():
        return "x"

    async def bad_undo(res):
        raise RuntimeError("wallet unreachable")

    async def fail():
        raise RuntimeError("boom")

    out = run(Saga([Step(name="grant", do=do, undo=bad_undo), Step(name="later", do=fail)]).run())
    # the work list carries the created resource's reference so resume() can retry the undo
    assert out.unfinished == [{"step": "grant", "error": "wallet unreachable", "reference": "x"}]


# ── declaration validation ────────────────────────────────────────────────────
def test_a_cycle_is_named_not_merely_detected():
    """'There is a cycle somewhere in nine requirements' is barely better than silence."""
    with pytest.raises(FlowError, match="a -> b -> a"):
        validate([{"flow": "f", "audience": {}, "requirements": [
            {"id": "a", "satisfied_by": "local.x", "blocks": ["b"]},
            {"id": "b", "satisfied_by": "local.y", "blocks": ["a"]}]}])


@pytest.mark.parametrize("flows,match", [
    ([{"flow": "f", "audience": {}, "requirements": [
        {"id": "a", "satisfied_by": "local.x", "blocks": ["ghost"]}]}], "not a requirement"),
    ([{"flow": "f", "audience": {}, "requirements": [
        {"id": "a", "satisfied_by": "local.x"}, {"id": "a", "satisfied_by": "local.y"}]}], "twice"),
    ([{"flow": "f", "audience": {}, "requirements": [
        {"id": "a", "satisfied_by": "crm.thing"}]}], "not a known source"),
    ([{"flow": "f", "audience": {}, "requirements": []}], "no requirements"),
    ([{"flow": "f", "audience": {}, "requirements": [
        {"id": "a", "satisfied_by": "local.x", "scope": "galaxy"}]}], "scope"),
])
def test_malformed_flows_fail_at_boot(flows, match):
    """Every one of these would otherwise strand a real person mid-onboarding — the worst place to
    find a typo, because the customer cannot proceed and cannot see why."""
    with pytest.raises(FlowError, match=match):
        validate(flows)


def test_the_shipped_flows_are_valid():
    from app.onboarding_flows import ONBOARDING_FLOWS
    validate(ONBOARDING_FLOWS)


# ── provisioning: the orphan the Saga exists to prevent ───────────────────────
class _Prov:
    driver = "fake"

    def __init__(self, kind, fail=False, undo_fail=False):
        self.kind, self.fail, self.undo_fail = kind, fail, undo_fail
        self.creates = self.undos = 0
        self.keys: list[str] = []

    async def create(self, req):
        self.creates += 1
        self.keys.append(req.key)
        from app.ports.provisioner import CREATED, FAILED, ProvisionResult
        if self.fail:
            return ProvisionResult(status=FAILED, detail="refused")
        return ProvisionResult(status=CREATED, reference=f"{self.kind}-1")

    async def undo(self, req, ref):
        from app.ports.provisioner import CREATED, FAILED, ProvisionResult
        self.undos += 1
        if self.undo_fail:
            return ProvisionResult(status=FAILED, detail="unreachable")
        return ProvisionResult(status=CREATED, reference=ref)


def _prov_service(provisioners, store=None):
    from app.adapters.datastore.memory import MemoryDataStore
    from app.core.provisioning import ProvisioningService

    class _Clock:
        def now(self):
            return 1000.0

    return ProvisioningService(provisioners=provisioners, store=store or MemoryDataStore(),
                               clock=_Clock())


def test_a_failed_later_step_leaves_no_orphan_tenant():
    """THE failure this exists to prevent: provision the tenant, register the owner, owner fails, tenant orphaned
    with no rollback and manual recovery. Made impossible."""
    tenant, membership = _Prov("tenant"), _Prov("membership", fail=True)
    svc = _prov_service({"tenant": tenant, "membership": membership})
    out = run(svc.provision(tenant="acme", subject="u1",
                            steps=[{"kind": "tenant"}, {"kind": "membership"}]))
    assert out["ok"] is False
    assert out["failed_step"] == "membership"
    assert out["compensated"] == ["tenant"]
    assert tenant.undos == 1


def test_provisioning_keys_are_stable_so_a_retry_does_not_double_create():
    """A customer who gets a timeout WILL press the button again."""
    first, second = _Prov("tenant"), _Prov("tenant")
    run(_prov_service({"tenant": first}).provision(
        tenant="acme", subject="u1", steps=[{"kind": "tenant"}]))
    run(_prov_service({"tenant": second}).provision(
        tenant="acme", subject="u1", steps=[{"kind": "tenant"}]))
    assert first.keys == second.keys        # same key -> the downstream service dedupes


def test_an_irreversible_step_is_recorded_as_residue():
    svc = _prov_service({"tenant": _Prov("tenant"), "email": _Prov("email"),
                         "membership": _Prov("membership", fail=True)})
    out = run(svc.provision(tenant="acme", subject="u2", steps=[
        {"kind": "tenant"},
        {"kind": "email", "irreversible": "an email cannot be unsent"},
        {"kind": "membership"}]))
    assert out["residue"] == [{"step": "email", "why": "an email cannot be unsent",
                               "reference": "email-1"}]


def test_a_failed_compensation_is_resumable_and_drains():
    from app.adapters.datastore.memory import MemoryDataStore
    store = MemoryDataStore()
    tenant = _Prov("tenant", undo_fail=True)
    svc = _prov_service({"tenant": tenant, "membership": _Prov("m", fail=True)}, store=store)
    out = run(svc.provision(tenant="acme", subject="u3",
                            steps=[{"kind": "tenant"}, {"kind": "membership"}]))
    assert [u["step"] for u in out["unfinished"]] == ["tenant"]

    tenant.undo_fail = False                 # the outage clears
    resumed = run(svc.resume("acme", "u3"))
    assert resumed["unfinished"] == []       # the queue drained


def test_a_missing_provisioner_refuses_before_creating_anything():
    """Discovering it halfway means compensating work we should never have started."""
    tenant = _Prov("tenant")
    out = run(_prov_service({"tenant": tenant}).provision(
        tenant="acme", subject="u4", steps=[{"kind": "tenant"}, {"kind": "ghost"}]))
    assert out["ok"] is False and tenant.creates == 0


def test_resume_does_not_re_run_the_forward_steps():
    """Re-running them would create exactly what the rollback is trying to remove."""
    from app.adapters.datastore.memory import MemoryDataStore
    store = MemoryDataStore()
    tenant = _Prov("tenant", undo_fail=True)
    svc = _prov_service({"tenant": tenant, "membership": _Prov("m", fail=True)}, store=store)
    run(svc.provision(tenant="acme", subject="u5",
                      steps=[{"kind": "tenant"}, {"kind": "membership"}]))
    creates_before = tenant.creates
    tenant.undo_fail = False
    run(svc.resume("acme", "u5"))
    assert tenant.creates == creates_before
