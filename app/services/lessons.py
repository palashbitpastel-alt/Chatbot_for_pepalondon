"""What the shop owner taught the assistant, by reviewing its answers.

The model itself is never retrained. Instead the owner marks real answers
good or wrong on the review page (see ``endpoints.review``), optionally writing
what it should have said. Each verdict is stored as a ``knowledge_chunks`` row
of kind ``lesson``, embedded on the moment it answered: the reply it was
answering plus the shopper's message, because "yes please" means nothing alone.

On every chat turn the same moment is embedded and the closest lessons are put
in front of the model as examples. The model decides whether one applies - a
lesson is guidance about how to answer, never a script, and live store data
(products, prices, stock) always wins over whatever an old answer said.
"""

import logging
from contextvars import ContextVar

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KnowledgeChunk
from app.services import rag

logger = logging.getLogger(__name__)

KIND = "lesson"
VERDICTS = ("good", "bad")
RECALL_LIMIT = 3
# Below this a lesson is about something else; the model still judges the rest.
MIN_SCORE = 0.35
MOMENT_CHARS = 400
TEXT_CHARS = 1500
# Off only while the replay test measures answers without the lessons.
enabled: ContextVar[bool] = ContextVar("lessons_enabled", default=True)


def reading(understood: dict | None) -> str:
    """The model's reading of what the shopper wants ("Age: 5Y; Occasion: Birthday").

    Filed and looked up alongside the words, so "a five-year-old" and "my 5 yo"
    meet on the same reading even though they share no words."""
    fields = (understood or {}).get("fields") or []
    return "; ".join(f'{f["label"]}: {f["value"]}' for f in fields if f.get("value"))


def moment(previous_reply: str | None, message: str, understood: dict | None = None) -> str:
    """The text a lesson is filed under and looked up by."""
    before = (previous_reply or "").strip()[-MOMENT_CHARS:]
    said = message.strip()[:MOMENT_CHARS]
    text = f"Assistant had said: {before}\nShopper: {said}" if before else f"Shopper: {said}"
    if read := reading(understood):
        text += f"\nWants: {read}"
    return text


async def save(db: AsyncSession, *, message_id: int, session_id: str, previous_reply: str | None,
               question: str, answer: str, verdict: str, correction: str = "", note: str = "",
               understood: dict | None = None) -> None:
    """File (or refile) the owner's verdict on one answer."""
    if verdict not in VERDICTS:
        raise ValueError("verdict must be good or bad")
    await forget(db, message_id)
    await rag._write_chunks(db, [{
        "kind": KIND,
        "ref_id": str(message_id),
        "content": moment(previous_reply, question, understood),
        "meta": {
            "verdict": verdict,
            "session_id": session_id,
            "question": question[:TEXT_CHARS],
            "answer": answer[:TEXT_CHARS],
            "correction": correction.strip()[:TEXT_CHARS],
            "note": note.strip()[:TEXT_CHARS],
        },
    }])
    await db.commit()


async def forget(db: AsyncSession, message_id: int) -> None:
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.kind == KIND,
                                                  KnowledgeChunk.ref_id == str(message_id)))
    await db.commit()


async def verdicts(db: AsyncSession, message_ids: list[int]) -> dict[int, dict]:
    if not message_ids:
        return {}
    rows = (await db.execute(select(KnowledgeChunk).where(
        KnowledgeChunk.kind == KIND, KnowledgeChunk.ref_id.in_([str(i) for i in message_ids])))).scalars()
    return {int(r.ref_id): (r.meta or {}) for r in rows}


def _example(n: int, meta: dict) -> str:
    lines = [f"Example {n} - shopper wrote: {meta.get('question', '')}"]
    if meta.get("verdict") == "good":
        lines.append(f"  Approved answer: {meta.get('answer', '')}")
    else:
        lines.append(f"  An answer the owner marked WRONG: {meta.get('answer', '')}")
        if meta.get("correction"):
            lines.append(f"  What the owner says it should have been: {meta['correction']}")
    if meta.get("note"):
        lines.append(f"  Owner's note: {meta['note']}")
    return "\n".join(lines)


async def recall(db: AsyncSession, previous_reply: str | None, message: str,
                 understood: dict | None = None) -> str:
    """The owner's reviewed examples closest to this moment, as a briefing note."""
    from app.services import shops
    if not enabled.get() or not shops.is_default():
        return ""  # reviewed on the default shop's answers; other shops have none yet
    try:
        hits = await rag.search(db, moment(previous_reply, message, understood), kinds=[KIND],
                                limit=RECALL_LIMIT, min_score=MIN_SCORE)
    except Exception:  # noqa: BLE001 - a lesson must never cost the answer
        logger.warning("Could not look up reviewed answers", exc_info=True)
        return ""
    if not hits:
        return ""
    body = "\n".join(_example(i, h.meta) for i, h in enumerate(hits, 1))
    return ("[The shop owner reviewed answers to similar messages. Where one really is the same "
            "situation, follow the owner's way of answering - tone, what to ask, what to show - and "
            "avoid what they marked wrong. Ignore any that do not fit. Products, prices and stock "
            "in these examples may be out of date: always use the store's live data.\n"
            f"{body}]")
