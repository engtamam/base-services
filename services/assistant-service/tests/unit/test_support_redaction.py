"""assistant-004/003 regressions: support triage/draft redact PII before the LLM; connection
routes are not default-open in production."""
from __future__ import annotations

import asyncio

from app.core import support
from app.core.internal_policy import InternalRoutePolicy
from app.ports.model import ModelAnswer


def run(c):
    return asyncio.run(c)


class _CapModel:
    driver = "http"           # generative, so triage/draft actually run

    def __init__(self):
        self.seen = []

    async def answer(self, *, question, passages, lang="en"):
        self.seen.append(passages[0].text)
        return ModelAnswer(text='{"type":"billing","priority":"normal","tags":[],'
                                '"confidence":0.9,"summary":"x"}', used_refs=())


def test_triage_redacts_pii_before_the_model():
    m = _CapModel()
    run(support.triage(m, current_type="", subject="card 4111111111111111",
                       body="my IBAN is SA0380000000608010167519 and phone +966501234567",
                       candidate_types=("billing",), redact_pii=True))
    sent = m.seen[0]
    assert "4111111111111111" not in sent
    assert "SA0380000000608010167519" not in sent
    assert "+966501234567" not in sent


def test_draft_redacts_pii_before_the_model():
    m = _CapModel()
    run(support.draft(m, subject="hi",
                      thread=[{"author": "cust", "body": "charge my card 4111111111111111"}],
                      redact_pii=True))
    assert "4111111111111111" not in m.seen[0]


def test_redaction_can_be_disabled_by_policy():
    m = _CapModel()
    run(support.triage(m, current_type="", subject="card 4111111111111111", body="x",
                       candidate_types=("billing",), redact_pii=False))
    assert "4111111111111111" in m.seen[0]     # policy off → passthrough (operator's choice)


# ── assistant-003: the connection routes are covered by an explicit policy ──────────────────────────
def test_policy_covers_reports_default_open_vs_locked():
    open_policy = InternalRoutePolicy(None)
    assert open_policy.covers("/internal/assistant/connections") is False
    locked = InternalRoutePolicy({"/internal/assistant/connections": ["ticket-service"]})
    assert locked.covers("/internal/assistant/connections") is True
    # longest-prefix: a sub-path is covered too
    assert locked.covers("/internal/assistant/connections/abc/invoke") is True
    assert locked.permits("/internal/assistant/connections/abc/invoke", "ticket-service") is True
    assert locked.permits("/internal/assistant/connections/abc/invoke", "evil-service") is False
