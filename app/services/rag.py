"""Retrieval-augmented memory for the admin agent, on pgvector.

``knowledge_chunks`` holds two kinds of knowledge:

* **Chat memory** - one chunk per admin conversation turn (question + answer),
  so the agent can recall what staff asked and were told earlier, across
  sessions.
* **Handbook** - the store's own handbook (see ``services.handbook``),
  re-embedded only when the file changes.

Live Shopify data (products, orders, campaigns, expenses, tasks) is fetched
fresh from Shopify on every read instead - see ``services.shopify_store`` and
``insights.search_store`` - so there is nothing to keep an index in sync with.

On Postgres the similarity search is a pgvector cosine-distance query (with an
HNSW index, see ``db.migrate``). On SQLite - local development only - vectors
are stored as JSON and ranked in Python, which is fine for a small store.
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import KnowledgeChunk
from app.services import embeddings

logger = logging.getLogger(__name__)

CHAT_KIND = "chat"
MAX_CHAT_CHUNK_CHARS = 1500


@dataclass
class Hit:
    kind: str
    content: str
    meta: dict
    score: float  # cosine similarity, higher is better
    session_id: str | None = None
    created_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _to_db_vector(vec: list[float]):
    return vec if settings.is_postgres else json.dumps(vec)


def _from_db_vector(value) -> list[float]:
    if value is None:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return [float(x) for x in value]


async def _write_chunks(db: AsyncSession, rows: list[dict]) -> int:
    if not rows:
        return 0
    vectors = await embeddings.embed([r["content"] for r in rows])
    model = embeddings.model_name()
    db.add_all(
        KnowledgeChunk(
            kind=r["kind"],
            ref_id=r.get("ref_id"),
            session_id=r.get("session_id"),
            content=r["content"],
            meta=r.get("meta"),
            embedding_model=model,
            embedding=_to_db_vector(v),
        )
        for r, v in zip(rows, vectors)
    )
    return len(rows)


async def remember_chat_turn(db: AsyncSession, session_id: str, question: str, answer: str) -> None:
    """Store one admin Q&A turn so later conversations can recall it."""
    content = f"Staff asked: {question.strip()}\nAgent answered: {answer.strip()}"
    if len(content) > MAX_CHAT_CHUNK_CHARS:
        content = content[: MAX_CHAT_CHUNK_CHARS - 1] + "…"
    await _write_chunks(
        db,
        [{"kind": CHAT_KIND, "session_id": session_id, "content": content, "meta": {"question": question[:200]}}],
    )
    await db.commit()


async def forget_session(db: AsyncSession, session_id: str) -> None:
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.session_id == session_id))
    await db.commit()


# --------------------------------------------------------------------------- #
# Searching
# --------------------------------------------------------------------------- #


async def search(
    db: AsyncSession,
    query: str,
    kinds: Iterable[str] | None = None,
    limit: int = 6,
    min_score: float = 0.0,
) -> list[Hit]:
    """Top-``limit`` chunks by cosine similarity to ``query``."""
    query = query.strip()
    if not query:
        return []
    vector = await embeddings.embed_one(query)
    model = embeddings.model_name()
    kind_list = list(kinds) if kinds else None

    if settings.is_postgres:
        distance = KnowledgeChunk.embedding.cosine_distance(vector).label("distance")
        stmt = select(KnowledgeChunk, distance).where(KnowledgeChunk.embedding_model == model)
        if kind_list:
            stmt = stmt.where(KnowledgeChunk.kind.in_(kind_list))
        stmt = stmt.order_by(distance).limit(limit)
        hits = [
            Hit(
                kind=row.kind,
                content=row.content,
                meta=row.meta or {},
                score=round(1.0 - float(dist), 4),
                session_id=row.session_id,
                created_at=row.created_at,
            )
            for row, dist in (await db.execute(stmt)).all()
        ]
    else:
        stmt = select(KnowledgeChunk).where(KnowledgeChunk.embedding_model == model)
        if kind_list:
            stmt = stmt.where(KnowledgeChunk.kind.in_(kind_list))
        scored: list[tuple[float, KnowledgeChunk]] = []
        for row in (await db.execute(stmt)).scalars():
            emb = _from_db_vector(row.embedding)
            if len(emb) != len(vector):
                continue
            scored.append((sum(a * b for a, b in zip(emb, vector)), row))
        scored.sort(key=lambda s: s[0], reverse=True)
        hits = [
            Hit(
                kind=row.kind,
                content=row.content,
                meta=row.meta or {},
                score=round(float(sim), 4),
                session_id=row.session_id,
                created_at=row.created_at,
            )
            for sim, row in scored[:limit]
        ]
    return [h for h in hits if h.score >= min_score]


async def build_context(db: AsyncSession, query: str, current_session: str | None = None) -> str:
    """Retrieved context to hand the admin agent alongside a question.

    Returns an empty string when nothing relevant is stored, so the caller can
    pass the question through untouched. Live store data is not attached here -
    it would mean a full Shopify fetch on every single chat message whether or
    not the question needs it; the agent pulls it on demand instead, via its
    ``get_*_data``/``search_store_knowledge`` tools.
    """
    memory_hits = await search(db, query, kinds=[CHAT_KIND], limit=settings.RAG_MEMORY_RESULTS + 2, min_score=0.1)
    # The current session's own turns are already in chat_history.
    memory_hits = [h for h in memory_hits if h.session_id != current_session][: settings.RAG_MEMORY_RESULTS]

    if not memory_hits:
        return ""
    return "Relevant earlier conversations with staff:\n" + "\n".join(
        f"- ({h.created_at.strftime('%Y-%m-%d') if h.created_at else 'earlier'}) {h.content}" for h in memory_hits
    )


async def stats(db: AsyncSession) -> dict:
    from sqlalchemy import func

    rows = (
        await db.execute(select(KnowledgeChunk.kind, func.count(KnowledgeChunk.id)).group_by(KnowledgeChunk.kind))
    ).all()
    return {
        "backend": "pgvector" if settings.is_postgres else "sqlite-json",
        "embedding_model": embeddings.model_name(),
        "chunks": {kind: count for kind, count in rows},
    }
