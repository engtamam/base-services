"""WhatsApp Flow provisioning — idempotent-by-name ensure over a WABA.

The behaviour that matters: a re-run at connect time must NOT create a duplicate Flow (a published
Flow is immutable and cannot be deleted), the create/clone source is exactly one, and a template can
reference the Flow by a name that is stable across every shop's clone.
"""
from __future__ import annotations

import pytest

from app.core.whatsapp_flows import FlowError, ensure_flow
from app.ports.whatsapp_flow_manager import DRAFT, PUBLISHED, FlowResult, FlowSummary


class _FakeManager:
    """Records calls and holds a name→(id,status) map, like a WABA's Flow list."""

    driver = "fake"

    def __init__(self, existing=None, create_status=DRAFT, create_ok=True):
        self._flows = dict(existing or {})   # name -> FlowSummary
        self.created = []
        self.published = []
        self._create_status = create_status
        self._create_ok = create_ok
        self._n = 0

    def list_flows(self):
        return list(self._flows.values())

    def create(self, *, name, categories, flow_json="", clone_flow_id="", endpoint_uri="",
               publish=False):
        self.created.append({"name": name, "flow_json": flow_json, "clone_flow_id": clone_flow_id,
                             "publish": publish, "categories": list(categories)})
        if not self._create_ok:
            return FlowResult(ok=False, name=name, reason="meta rejected",
                              validation_errors=("bad screen",))
        self._n += 1
        fid = f"flow:{self._n}"
        status = PUBLISHED if (publish and self._create_status == PUBLISHED) else DRAFT
        self._flows[name] = FlowSummary(id=fid, name=name, status=status)
        return FlowResult(ok=True, flow_id=fid, name=name, status=status)

    def publish(self, *, flow_id):
        self.published.append(flow_id)
        return FlowResult(ok=True, flow_id=flow_id, status=PUBLISHED)


def test_create_from_json_then_publishes_when_create_did_not():
    m = _FakeManager(create_status=DRAFT)   # create returns a draft → an explicit publish follows
    r = ensure_flow(manager=m, name="rating", categories=["SURVEY"], flow_json='{"x":1}')
    assert r.ok and r.created and r.published and r.status == PUBLISHED
    assert m.created[0]["flow_json"] == '{"x":1}' and m.created[0]["clone_flow_id"] == ""
    assert m.published == [r.flow_id]


def test_create_that_publishes_in_one_call_skips_the_extra_publish():
    m = _FakeManager(create_status=PUBLISHED)   # create+publish in one shot
    r = ensure_flow(manager=m, name="rating", categories=["SURVEY"], flow_json='{"x":1}')
    assert r.ok and r.created and r.published and m.published == []   # no second call


def test_clone_from_master_uses_clone_flow_id():
    m = _FakeManager()
    r = ensure_flow(manager=m, name="rating", categories=["SURVEY"], clone_flow_id="master:9")
    assert r.ok and r.created
    assert m.created[0]["clone_flow_id"] == "master:9" and m.created[0]["flow_json"] == ""


def test_idempotent_by_name_an_existing_published_flow_is_not_recreated():
    existing = {"rating": FlowSummary(id="flow:existing", name="rating", status=PUBLISHED)}
    m = _FakeManager(existing=existing)
    r = ensure_flow(manager=m, name="rating", categories=["SURVEY"], clone_flow_id="master:9")
    assert r.ok and r.created is False and r.flow_id == "flow:existing" and r.published
    assert m.created == [] and m.published == []   # nothing recreated, nothing re-published


def test_an_existing_draft_is_published_not_recreated():
    existing = {"rating": FlowSummary(id="flow:d", name="rating", status=DRAFT)}
    m = _FakeManager(existing=existing)
    r = ensure_flow(manager=m, name="rating", categories=["SURVEY"], flow_json='{"x":1}')
    assert r.ok and r.created is False and r.published and m.created == [] and m.published == ["flow:d"]


def test_exactly_one_source_is_required():
    m = _FakeManager()
    with pytest.raises(FlowError):   # neither
        ensure_flow(manager=m, name="r", categories=["SURVEY"])
    with pytest.raises(FlowError):   # both
        ensure_flow(manager=m, name="r", categories=["SURVEY"], flow_json="{}", clone_flow_id="x")


def test_categories_are_validated_against_metas_set():
    m = _FakeManager()
    with pytest.raises(FlowError):
        ensure_flow(manager=m, name="r", categories=[], flow_json="{}")
    with pytest.raises(FlowError):
        ensure_flow(manager=m, name="r", categories=["PROMО"], flow_json="{}")   # not a real category


def test_a_failed_create_reports_the_reason_and_validation_errors():
    m = _FakeManager(create_ok=False)
    r = ensure_flow(manager=m, name="r", categories=["SURVEY"], flow_json="{}")
    assert r.ok is False and r.created is False and "rejected" in r.reason
    assert r.validation_errors == ("bad screen",) and m.published == []
