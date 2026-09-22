"""Tiny forward-only schema upkeep, run at startup after ``create_all``.

``create_all`` only creates missing tables, so columns added to an existing table
would be silently absent on a database that already has data (the Railway
Postgres, a developer's SQLite file). This adds them. Anything more involved
belongs in Alembic; for now the app only ever *adds* nullable/defaulted columns.
"""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import settings

logger = logging.getLogger(__name__)

# table -> column -> DDL type + default (SQL fragments, no user input)
# Empty for now: every table that used to need forward-migrated columns
# (products/campaigns/orders/ops_tasks/expenses) was dropped when the app
# stopped persisting Shopify data - see services.shopify_store. Add entries
# here again if a future *persisted* table needs a new column.
ADDED_COLUMNS: dict[str, dict[str, str]] = {}


def _existing_columns(sync_conn) -> dict[str, set[str]]:
    insp = inspect(sync_conn)
    return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names()}


async def ensure_columns(conn: AsyncConnection) -> None:
    existing = await conn.run_sync(_existing_columns)
    for table, columns in ADDED_COLUMNS.items():
        have = existing.get(table)
        if have is None:
            continue  # create_all makes new tables with every column already
        for column, ddl in columns.items():
            if column in have:
                continue
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            logger.info("Added column %s.%s", table, column)


async def ensure_vector_index(conn: AsyncConnection) -> None:
    """An HNSW cosine index on the knowledge embeddings (Postgres + pgvector only)."""
    if not settings.is_postgres:
        return
    try:
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding "
                "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
            )
        )
    except Exception:
        logger.warning("Could not create the pgvector index; searches will scan", exc_info=True)
