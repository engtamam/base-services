"""The HTTP model provider — both wire shapes, and every failure path.

No vendor SDK and no live provider: the transport is injected, so these tests describe the WIRE
CONTRACT rather than any company's client library. That is also why one adapter can serve several
vendors — the difference between them is a payload shape, not a protocol.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.model.http_provider import CHAT, GENERATE, HttpModel
from app.ports.errors import ConfigError
from app.ports.model import ModelError, Passage

PASSAGES = (
    Passage(ref="billing.vat#0", title="VAT number", text="Your VAT number is fifteen digits."),
    Passage(ref="billing.refund#0", title="Refunds", text="A refund takes ten days."),
)


def run(c):
    return asyncio.run(c)


def _chat_body(text, tin=11, tout=7):
    return {"choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": tin, "completion_tokens": tout}}


def _gen_body(text, tin=11, tout=7):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": tin, "candidatesTokenCount": tout}}


def _model(body, status=200, shape=CHAT, capture=None, **kw):
    async def transport(url, headers, payload, timeout):
        if capture is not None:
            capture.update({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return status, body
    return HttpModel("https://provider/v1", "k", shape=shape, transport=transport, **kw)


def _ask(model):
    return run(model.answer(question="what is my vat number", passages=PASSAGES, lang="en"))


# ── both shapes ───────────────────────────────────────────────────────────────
def test_the_chat_shape_is_read():
    a = _ask(_model(_chat_body("It is fifteen digits [1].")))
    assert "fifteen digits" in a.text and a.tokens_in == 11 and a.tokens_out == 7


def test_the_generate_shape_is_read():
    a = _ask(_model(_gen_body("It is fifteen digits [1]."), shape=GENERATE))
    assert "fifteen digits" in a.text and a.tokens_in == 11 and a.tokens_out == 7


def test_the_chat_payload_carries_messages():
    cap = {}
    _ask(_model(_chat_body("x [1]"), capture=cap))
    assert [m["role"] for m in cap["payload"]["messages"]] == ["system", "user"]


def test_the_generate_payload_carries_contents_and_system_instruction():
    cap = {}
    _ask(_model(_gen_body("x [1]"), shape=GENERATE, capture=cap))
    assert cap["payload"]["contents"][0]["parts"][0]["text"]
    assert cap["payload"]["systemInstruction"]["parts"][0]["text"]


def test_an_unknown_shape_is_refused_at_construction():
    # Guessing a shape means an odd payload reads as "no answer" — an abstention that sends
    # somebody hunting a thin KB when the fault is a schema mismatch.
    with pytest.raises(ConfigError):
        HttpModel("https://x", shape="whatever")


def test_an_empty_url_is_refused():
    with pytest.raises(ConfigError):
        HttpModel("")


# ── citations ─────────────────────────────────────────────────────────────────
def test_numbered_markers_map_back_to_refs():
    """Numbers rather than raw ids: a model asked to reproduce 'billing.vat#0' mistypes it, and a
    mistyped ref fails the citation check and costs a CORRECT answer."""
    a = _ask(_model(_chat_body("See [1] and [2].")))
    assert set(a.used_refs) == {"billing.vat#0", "billing.refund#0"}


def test_only_cited_passages_are_reported():
    a = _ask(_model(_chat_body("Only [2] matters.")))
    assert a.used_refs == ("billing.refund#0",)


def test_an_out_of_range_citation_is_dropped():
    # [9] is the model inventing a source; the service's own check drops it again.
    a = _ask(_model(_chat_body("As shown in [9].")))
    assert a.used_refs == ()


def test_the_passages_are_numbered_in_the_prompt():
    cap = {}
    _ask(_model(_chat_body("x [1]"), capture=cap))
    user = cap["payload"]["messages"][1]["content"]
    assert "[1] VAT number" in user and "[2] Refunds" in user


def test_the_prompt_asks_for_grounding():
    cap = {}
    _ask(_model(_chat_body("x [1]"), capture=cap))
    system = cap["payload"]["messages"][0]["content"]
    assert "ONLY" in system and "do not know" in system.lower()


def test_a_persona_is_appended_not_substituted():
    """An operator's tone must not be able to delete the grounding instruction."""
    cap = {}
    run(_model(_chat_body("x [1]"), capture=cap).answer(
        question="q", passages=PASSAGES, lang="en", persona="Be brief and formal."))
    system = cap["payload"]["messages"][0]["content"]
    assert "ONLY" in system and "Be brief and formal." in system


# ── failures never leak ───────────────────────────────────────────────────────
def test_a_non_200_raises_without_the_body():
    """Provider bodies carry hostnames, model names, quota details and sometimes prompt
    fragments."""
    m = _model({"error": {"message": "quota exceeded for project acme-prod-1234",
                          "host": "internal-eu-3.provider.local"}}, status=429)
    with pytest.raises(ModelError) as e:
        _ask(m)
    msg = str(e.value)
    assert "429" in msg
    assert "acme-prod" not in msg and "internal-eu-3" not in msg


def test_a_transport_failure_becomes_a_model_error():
    async def boom(url, headers, payload, timeout):
        raise TimeoutError("connect timeout to 10.0.0.5:443")
    m = HttpModel("https://p", transport=boom)
    with pytest.raises(ModelError):
        _ask(m)


def test_a_200_with_no_text_is_a_failure_not_an_empty_answer():
    # Returning it as an answer hides that the provider misbehaved: the honesty gate would report
    # "unsupported", which is true and points at the wrong component.
    with pytest.raises(ModelError, match="no text"):
        _ask(_model(_chat_body("")))


def test_an_unparseable_body_is_a_failure():
    with pytest.raises(ModelError):
        _ask(_model(None))


def test_missing_usage_is_zero_rather_than_an_error():
    # Token counts are for metering, not correctness; a provider omitting them must not fail a
    # perfectly good answer.
    a = _ask(_model({"choices": [{"message": {"content": "Fine [1]."}}]}))
    assert a.tokens_in == 0 and a.tokens_out == 0 and a.text


def test_the_configured_timeout_reaches_the_transport():
    cap = {}
    _ask(_model(_chat_body("x [1]"), capture=cap, timeout=3.5))
    assert cap["timeout"] == 3.5


def test_the_api_key_is_sent_as_a_bearer_and_not_in_the_payload():
    cap = {}
    _ask(_model(_chat_body("x [1]"), capture=cap))
    assert cap["headers"]["Authorization"] == "Bearer k"
    assert "k" not in str(cap["payload"].get("model", ""))


# ── the guarantee that must survive a provider swap ───────────────────────────
def test_a_provider_that_ignores_the_instruction_is_still_caught():
    """THE POINT OF VERIFYING RATHER THAN INSTRUCTING. The prompt asks the model to answer only
    from the passages. A model that ignores it — answering fluently and citing nothing — must be
    downgraded by the service, not trusted because the prompt was polite."""
    import asyncio as _a

    from app.core.answering import UNSUPPORTED, compose
    from app.core.indexing import chunk_all
    from app.core.kb import parse_kb
    from app.core.retrieval import KbIndex

    doc = {"billing.vat": {"title": "VAT number", "body": "Your VAT number is fifteen digits."}}
    entries = parse_kb(doc)
    idx = KbIndex(entries, chunk_all(entries, max_chars=800))
    hits = idx.search("vat number")

    # A provider answering confidently with no citation at all.
    ungrounded = _model(_chat_body("Refunds are automatic and instant."))
    answer = _a.run(compose(question="vat", hits=hits, model=ungrounded, lang="en",
                            not_found_text="I don't know."))
    assert answer.abstained and answer.abstained_because == UNSUPPORTED
    assert "automatic" not in answer.text, "the fluent invention was shown to the user"


def test_a_provider_failure_abstains_rather_than_500s():
    import asyncio as _a

    from app.core.answering import MODEL_FAILED, compose
    from app.core.indexing import chunk_all
    from app.core.kb import parse_kb
    from app.core.retrieval import KbIndex

    doc = {"a.b": {"title": "T", "body": "Body text here."}}
    entries = parse_kb(doc)
    hits = KbIndex(entries, chunk_all(entries, max_chars=800)).search("body")
    broken = _model({"error": "boom"}, status=500)
    answer = _a.run(compose(question="q", hits=hits, model=broken, lang="en",
                            not_found_text="I don't know."))
    assert answer.abstained and answer.abstained_because == MODEL_FAILED
    assert answer.text == "I don't know."
