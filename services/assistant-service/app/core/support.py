"""Customer-service AI — optional ticket triage + reply drafting.

The future base-services intends: an operator turns on AI for customer service, and the assistant
triages a new ticket and drafts a reply an agent edits and sends. These helpers give ticket-service
(or any caller) that capability over the internal tier, reusing the assistant's configured Model —
but framing the TICKET itself as the model's evidence (a Passage), because a support ticket is
answered from its own content, not from the knowledge base.

TWO SAFETIES, because this is ADVISORY by contract (the caller treats a null/empty result as a shrug
and proceeds without a suggestion — never an error):

  * It needs a GENERATIVE model. The default `extractive` driver quotes passages, it does not write,
    so triage/draft return nothing under it — the suggestion appears only when a real LLM is wired
    (MODEL_DRIVER=http). No half-useful echo of the ticket back at the agent.
  * A malformed model reply is a shrug. The model is asked for JSON; anything unparseable yields an
    empty result rather than a 500 — a failed suggestion must never fail the ticket.
"""
from __future__ import annotations

import json
from typing import Any

from app.ports.model import Passage


def _generative(model: Any) -> bool:
    # Only a real generative model can triage/draft; the extractive default quotes, it cannot write.
    return getattr(model, "driver", "") not in ("", "extractive", "null")


def _json_object(text: str) -> dict:
    """Parse the first {...} object out of a model reply (tolerating prose / code fences)."""
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b <= a:
        return {}
    try:
        obj = json.loads(text[a:b + 1])
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


async def triage(model: Any, *, current_type: str, subject: str, body: str,
                 candidate_types: tuple[str, ...], lang: str = "en") -> dict:
    """Suggest {type, priority, tags, confidence, summary} for a new ticket. {} means no suggestion."""
    if not _generative(model):
        return {}
    types = ", ".join(candidate_types) or "(any)"
    question = (
        "Triage this customer support ticket. Reply with ONLY a JSON object and nothing else: "
        '{"type": <one of the candidate types>, "priority": "low"|"normal"|"high"|"urgent", '
        '"tags": [up to 5 short lowercase tags], "confidence": 0.0-1.0, '
        '"summary": "one sentence"}. '
        f"Candidate types: {types}.")
    passage = Passage(ref="ticket", title=subject or "ticket",
                      text=f"Subject: {subject}\n\n{body}")
    ans = await model.answer(question=question, passages=(passage,), lang=lang)
    obj = _json_object(ans.text or "")
    if not obj:
        return {}
    return {
        "type": str(obj.get("type") or current_type or ""),
        "priority": str(obj.get("priority") or ""),
        "tags": [str(t) for t in (obj.get("tags") or [])][:5],
        "confidence": float(obj.get("confidence") or 0.0),
        "summary": str(obj.get("summary") or ""),
    }


async def draft(model: Any, *, subject: str, thread: list[dict], lang: str = "en") -> dict:
    """Draft a reply for an agent to review. {} means no draft (no generative model, or nothing back)."""
    if not _generative(model):
        return {}
    convo = "\n".join(f"{m.get('author', '')}: {m.get('body', '')}"
                      for m in (thread or []) if isinstance(m, dict))
    question = ("Draft a concise, professional reply a support agent will review before sending. "
                "Reply with ONLY the message text — no preamble, no signature placeholder.")
    passage = Passage(ref="thread", title=subject or "ticket",
                      text=f"Subject: {subject}\n\n{convo}")
    ans = await model.answer(question=question, passages=(passage,), lang=lang)
    text = (ans.text or "").strip()
    if not text:
        return {}
    # Advisory confidence: a human reviews every draft before it is sent, so this is a hint, not a gate.
    return {"text": text, "confidence": 0.6}
