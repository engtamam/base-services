"""The assistant's user-facing tier: ask a question, or fetch the help for one UI element.

TWO ROUTES, BECAUSE THERE ARE TWO QUESTIONS. A `?` icon knows exactly which entry it wants — that
is a dictionary lookup and must not cost a retrieval pass, a model call, or any latency the user
can feel. A typed question does not know, and needs ranking. Collapsing both into one "ask"
endpoint would make the cheap, common case pay for the expensive, rare one.

SURFACE IS TAKEN FROM THE REQUEST; CAPABILITIES ARE TAKEN FROM THE TOKEN. That split is the whole
authorisation story here. A client legitimately knows which of an adopter's UIs it is, so it says
so — but it says so as a hint about what is RELEVANT, and it is never trusted about what is
ALLOWED. Entries gate on capability, which comes from the verified token and nowhere else.

DEEP LINKS, NEVER EXECUTION. When the evidence includes a declared action, the response carries
where that action lives so the UI can offer a button. The assistant does not perform it: acting on
a misread question is a large surface for a small gain, and "here is the screen" is nearly all of
the value.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.kb_service import KbService
from app.core.pii import subject_ref
from app.core.quota import DailyQuota
from app.core.redaction import redact

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])


def build_kb(request: Request) -> KbService:
    c = request.app.state.container
    s = get_settings()
    return KbService(
        source=c.kb_source,
        model=c.model,
        cfg={"chunk_max_chars": s.KB_CHUNK_MAX_CHARS,
             "top_n": s.RETRIEVAL_TOP_N,
             "min_score": s.RETRIEVAL_MIN_SCORE,
             "max_context_chars": s.RETRIEVAL_MAX_CONTEXT_CHARS,
             "bm25_k1": s.RETRIEVAL_BM25_K1,
             "bm25_b": s.RETRIEVAL_BM25_B,
             "max_answer_tokens": s.MODEL_MAX_ANSWER_TOKENS},
    )


class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    # Which of the adopter's UIs is asking. A relevance hint, never a permission.
    surface: str = Field(default="", max_length=64)
    lang: str = Field(default="", max_length=16)
    # NOTE: there is deliberately no `kb` field. The knowledge comes from a configured source, so
    # no caller can supply the facts the product appears to assert.


def _actions(hits) -> list[dict]:
    """Deep links for any declared action among the evidence."""
    out = []
    for hit in hits:
        meta = hit.entry.metadata or {}
        if hit.entry.kind != "action" or not meta.get("path"):
            continue
        out.append({"id": meta.get("action_id") or hit.entry.id,
                    "label": hit.entry.text("en")[0],
                    "method": meta.get("method", ""), "path": meta["path"],
                    "service": meta.get("service", ""), "capability": hit.entry.requires})
    return out


@router.post("/ask", dependencies=[Depends(require_capability("assistant.ask")),
                                  Depends(rate_limit)])
async def ask(request: Request, body: AskIn,
              principal: Principal = Depends(require_user)) -> dict:
    """Answer from the knowledge base — grounded and cited, or an honest abstention."""
    s = get_settings()
    # The pseudonym identifies the spender without naming them; the quota store therefore holds no
    # identities either.
    spender = subject_ref(s.PII_SALT, principal.tenant or "", principal.subject)         if s.PII_SALT else principal.subject
    quota = DailyQuota(request.app.state.container.rate_limiter,
                       per_subject=s.QUOTA_DAILY_PER_SUBJECT,
                       per_tenant=s.QUOTA_DAILY_PER_TENANT)
    verdict = await quota.spend(tenant=principal.tenant or "", subject_ref=spender)
    if not verdict.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"DAILY_QUOTA_EXCEEDED:{verdict.scope}",
            headers={"Retry-After": str(verdict.retry_after_s or 3600)})

    # Redacted BEFORE anything: before it reaches a third-party model, before it is stored, and
    # before it is ranked. Redacting later would mean the raw text had already left the process.
    cleaned = redact(body.question, enabled=s.REDACT_PII, max_chars=s.REDACT_MAX_CHARS)
    kb = build_kb(request)
    answer, hits = await kb.ask(
        question=cleaned.text,
        surface=body.surface,
        caps=principal.caps,          # from the verified token, never from the body
        lang=body.lang or s.DEFAULT_LANGUAGE,
        not_found_text=s.ASSISTANT_NOT_FOUND_TEXT,
    )
    if answer.abstained:
        # Refunded: an abstention did no work worth paying for, and charging for one would mean a
        # user burns their day discovering a gap in the knowledge base.
        await quota.refund(tenant=principal.tenant or "", subject_ref=spender)

    if answer.abstained:
        # NON-CRITICAL MEANS THE ANSWER SURVIVES THE LOG FAILING. This block is best-effort in
        # full: production refuses to boot without PII_SALT, but a development run has none, and
        # pseudonymising would raise — turning a help question into a 500 to protect a record of
        # who asked it. The declaration says this action is not worth that, so the code must agree.
        # The questions the assistant could NOT answer are the highest-value signal for what the
        # knowledge base is missing, and they are invisible anywhere else. Recorded AFTER the fact
        # and non-critically: refusing to answer a help question because the audit log is
        # unreachable would take the product down to protect a record of who asked what.
        #
        # The QUESTION is not recorded — only that one was asked and why it failed. A help question
        # is user content and often carries the very identifiers the platform works hard to keep
        # out of an append-only log.
        # The actor is a PSEUDONYM, never the subject: this lands in an append-only chain, and a
        # record that names the person asking is a record nobody can later erase.
        try:
            actor = subject_ref(s.PII_SALT, principal.tenant or "", principal.subject)
            await request.app.state.auditor.record(
                "assistant.abstained", actor=actor, tenant=principal.tenant or "",
                resource=f"surface:{body.surface or 'default'}",
                ip=request.client.host if request.client else "",
                metadata={"reason": answer.abstained_because, "searched": len(hits)})
        except Exception:   # noqa: BLE001 — see above: the answer outranks the record here
            pass

    return {
        "answer": answer.text,
        "grounded": answer.grounded,
        # Present and empty on an abstention, so a client never has to distinguish "no citations"
        # from "the field is missing".
        "citations": [{"ref": c.ref, "entry_id": c.entry_id, "title": c.title}
                      for c in answer.citations],
        "actions": _actions(hits) if answer.grounded else [],
        "abstained_because": answer.abstained_because,
        "searched": len(hits),
        # Told, not hidden. A user who pasted a card number should see that we removed it, and an
        # answer that seems to ignore the question is otherwise baffling.
        "redacted": list(cleaned.hits),
    }


@router.get("/help", dependencies=[Depends(require_capability("assistant.ask")),
                                  Depends(rate_limit)])
async def help_for_anchor(request: Request, anchor: str = "", surface: str = "", lang: str = "",
                          principal: Principal = Depends(require_user)) -> dict:
    """The entry behind a `?` icon. No ranking, no model, no cost."""
    if not anchor.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ANCHOR_REQUIRED")
    s = get_settings()
    entry = await build_kb(request).lookup(anchor=anchor, surface=surface, caps=principal.caps)
    if entry is None:
        # 404 whether it does not exist or the caller may not see it: distinguishing them would
        # confirm that an admin-only control exists to someone who cannot use it.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    title, text = entry.text(lang or s.DEFAULT_LANGUAGE)
    return {"id": entry.id, "anchor": entry.anchor, "title": title, "body": text,
            "kind": entry.kind, "related": list(entry.related)}


# ── conversations ─────────────────────────────────────────────────────────────
# Absent entirely when history is switched off: a 404 rather than an empty list, because "you have
# no conversations" and "this deployment keeps none" are different facts and a client that cannot
# tell them apart will render an empty history as data loss.
def _conversations(request: Request):
    store = request.app.state.container.conversations
    if store is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CONVERSATIONS_DISABLED")
    return store


@router.get("/conversations", dependencies=[Depends(require_capability("assistant.ask")),
                                            Depends(rate_limit)])
async def list_conversations(request: Request, after: str = "",
                             principal: Principal = Depends(require_user)) -> dict:
    """This caller's own conversations. Scoped at the store by tenant AND subject."""
    s = get_settings()
    store = _conversations(request)
    convs, cursor = await store.list(tenant=principal.tenant or "", subject=principal.subject,
                                     limit=s.CONVERSATION_PAGE_SIZE, after=after or None)
    return {"conversations": [{"id": c.id, "title": c.title, "surface": c.surface,
                               "created": c.created, "updated": c.updated} for c in convs],
            "next": cursor}


@router.get("/conversations/{conversation_id}",
            dependencies=[Depends(require_capability("assistant.ask")), Depends(rate_limit)])
async def get_conversation(request: Request, conversation_id: str,
                           principal: Principal = Depends(require_user)) -> dict:
    """One conversation with its messages — the caller's own, or a 404."""
    store = _conversations(request)
    tenant, subject = principal.tenant or "", principal.subject
    conv = await store.get(conversation_id=conversation_id, tenant=tenant, subject=subject)
    if conv is None:
        # 404 for "not yours" as well as "not there": distinguishing them confirms the id exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    msgs, cursor = await store.messages(conversation_id=conversation_id, tenant=tenant,
                                        subject=subject)
    return {"id": conv.id, "title": conv.title,
            "messages": [{"id": m.id, "role": m.role, "content": m.content,
                          "created": m.created, "citations": list(m.citations)} for m in msgs],
            "next": cursor}


@router.delete("/conversations/{conversation_id}",
               dependencies=[Depends(require_capability("assistant.ask")), Depends(rate_limit)])
async def delete_conversation(request: Request, conversation_id: str,
                              principal: Principal = Depends(require_user)) -> dict:
    """A real delete, not an archive flag. Asked to be forgotten means gone."""
    store = _conversations(request)
    ok = await store.delete(conversation_id=conversation_id, tenant=principal.tenant or "",
                            subject=principal.subject)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return {"deleted": conversation_id}
