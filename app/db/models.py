from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base


class StoreSetting(Base):
    """Small key/value app settings unrelated to any Shopify data - e.g. the
    handbook's content hash, so it's only re-embedded when the file changes
    (see ``services.handbook``)."""

    __tablename__ = "store_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(10))  # user|assistant
    content: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ShopperState(Base):
    """A signed-in shopper's own chat state, so it follows them between devices.

    Their recent chats (with pins), the pieces they saved, what we remember about
    who they shop for, and the cards each chat drew. It is written by the widget
    and read straight back - the server never reads inside it - and it is keyed
    by the customer id the theme SIGNED, so one shopper can never read another's.
    """

    __tablename__ = "shopper_state"

    customer_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class OrderChangeRequest(Base):
    """A cancellation or address change a shopper has started but not confirmed.

    Keyed by the chat session rather than by a token the agent carries, because
    the agent cannot carry one: only the user and assistant turns are replayed
    into its context, so a tool result minted three turns ago is long gone by the
    time the shopper says "yes". Keying it server-side also means the secret
    never enters the model's context at all.

    In the database rather than in memory so it survives a redeploy - Railway
    restarts on every push, and an in-process dict loses every pending change.

    One row per session: starting a new change replaces the old one.
    """

    __tablename__ = "order_change_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    order_gid: Mapped[str] = mapped_column(String(128))
    order_name: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(20))  # cancel|change_address
    email: Mapped[str] = mapped_column(String(255))

    challenge_kind: Mapped[str] = mapped_column(String(16))  # postcode|total
    # SHA-256 of the normalised answer. A postcode is order PII, and nothing here
    # needs to read it back - only compare against it.
    challenge_hash: Mapped[str] = mapped_column(String(64))

    current_address: Mapped[dict] = mapped_column(JSON, default=dict)
    order_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    verified: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


def _embedding_column_type():
    """pgvector's Vector on Postgres; a JSON-text column on SQLite so local
    development works without the extension (see services.rag)."""
    if settings.is_postgres:
        from pgvector.sqlalchemy import Vector

        return Vector(settings.EMBEDDING_DIMENSIONS)
    return Text


class KnowledgeChunk(Base):
    """One embedded piece of knowledge the admin agent can retrieve.

    ``kind`` is ``chat`` for a remembered admin conversation turn (accumulates as
    the agent is used) or ``handbook`` for the store's handbook (re-embedded only
    when the file changes, see ``services.handbook``). Live Shopify data - products,
    orders, campaigns, expenses, tasks - is fetched fresh on every read instead of
    being embedded here; see ``services.shopify_store``.
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_knowledge_chunks_kind_ref", "kind", "ref_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(100))
    embedding = mapped_column(_embedding_column_type(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
