"""The answer review page: the shop owner teaches the assistant by example.

GET  /review                 the page itself (asks for the review key)
GET  /review/conversations   recent shopper conversations, each answer with its verdict
POST /review/verdict         mark one answer good or wrong, with an optional correction
DELETE /review/verdict/{id}  take a verdict back

Every data call needs the ``X-Review-Key`` header to equal REVIEW_KEY; with no
REVIEW_KEY set the page stays closed. See ``services.lessons`` for how verdicts
reach later answers.
"""

import hmac
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.v1.endpoints.support import SESSION_PREFIX, _without_cards_note
from app.core.config import settings
from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal
from app.services import lessons

router = APIRouter(tags=["review"])

PAGE = Path(__file__).with_name("review_page.html")


def _check(key: str | None) -> None:
    if not settings.REVIEW_KEY:
        raise HTTPException(status_code=503, detail="Set REVIEW_KEY on the server to open the review page.")
    if not key or not hmac.compare_digest(key.encode(), settings.REVIEW_KEY.encode()):
        raise HTTPException(status_code=401, detail="Wrong review key.")


class Verdict(BaseModel):
    message_id: int
    verdict: str = Field(pattern="^(good|bad)$")
    correction: str = Field(default="", max_length=3000)
    note: str = Field(default="", max_length=1000)


@router.get("/review", response_class=HTMLResponse, include_in_schema=False)
async def review_page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@router.get("/review/conversations")
async def conversations(
    x_review_key: str | None = Header(default=None),
    limit: int = Query(15, ge=1, le=50),
    offset: int = Query(0, ge=0),
) -> dict:
    """The latest shopper conversations, newest first, each turn with its verdict."""
    _check(x_review_key)
    async with AsyncSessionLocal() as db:
        latest = func.max(ChatMessage.id).label("latest")
        sessions = (await db.execute(
            select(ChatMessage.session_id, latest)
            .where(ChatMessage.session_id.like(f"{SESSION_PREFIX}%"))
            .group_by(ChatMessage.session_id)
            .order_by(latest.desc())
            .limit(limit).offset(offset)
        )).all()
        ids = [s for s, _ in sessions]
        rows = (await db.execute(
            select(ChatMessage).where(ChatMessage.session_id.in_(ids)).order_by(ChatMessage.id.asc())
        )).scalars().all() if ids else []
        answer_ids = [m.id for m in rows if m.role == "assistant"]
        marked = await lessons.verdicts(db, answer_ids)

    by_session: dict[str, list[ChatMessage]] = {s: [] for s in ids}
    for m in rows:
        by_session[m.session_id].append(m)
    out = []
    for sid in ids:
        turns = []
        for m in by_session[sid]:
            turn = {"id": m.id, "role": m.role, "content": _without_cards_note(m.content),
                    "at": m.created_at.isoformat() if m.created_at else None}
            if m.role == "assistant" and m.id in marked:
                meta = marked[m.id]
                turn["review"] = {k: meta.get(k, "") for k in ("verdict", "correction", "note")}
            turns.append(turn)
        out.append({"session_id": sid, "messages": turns})
    return {"conversations": out, "more": len(ids) == limit}


@router.post("/review/verdict")
async def save_verdict(body: Verdict, x_review_key: str | None = Header(default=None)) -> dict:
    _check(x_review_key)
    async with AsyncSessionLocal() as db:
        answer = await db.get(ChatMessage, body.message_id)
        if answer is None or answer.role != "assistant" or not answer.session_id.startswith(SESSION_PREFIX):
            raise HTTPException(status_code=404, detail="No such answer.")
        before = (await db.execute(
            select(ChatMessage).where(ChatMessage.session_id == answer.session_id, ChatMessage.id < answer.id)
            .order_by(ChatMessage.id.desc()).limit(2)
        )).scalars().all()
        question = next((m.content for m in before if m.role == "user"), "")
        if not question:
            raise HTTPException(status_code=422, detail="That answer has no question before it.")
        earlier = (await db.execute(
            select(ChatMessage).where(ChatMessage.session_id == answer.session_id,
                                      ChatMessage.role == "assistant", ChatMessage.id < answer.id)
            .order_by(ChatMessage.id.desc()).limit(1)
        )).scalars().first()
        if body.verdict == "bad" and not (body.correction.strip() or body.note.strip()):
            raise HTTPException(status_code=422, detail="Say what was wrong or what it should have said.")
        await lessons.save(
            db, message_id=answer.id, session_id=answer.session_id,
            previous_reply=_without_cards_note(earlier.content) if earlier else None,
            question=question, answer=_without_cards_note(answer.content),
            verdict=body.verdict, correction=body.correction, note=body.note,
        )
    return {"saved": True}


@router.delete("/review/verdict/{message_id}")
async def remove_verdict(message_id: int, x_review_key: str | None = Header(default=None)) -> dict:
    _check(x_review_key)
    async with AsyncSessionLocal() as db:
        await lessons.forget(db, message_id)
    return {"removed": True}
