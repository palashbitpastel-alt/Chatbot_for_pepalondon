import os

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict:
    # Which commit is live - Railway sets this on every deploy - so "is my push
    # deployed yet?" is one request away.
    return {"status": "ok", "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7] or None}


@router.get("/health/db")
async def db_health_check(db: AsyncSession = Depends(get_db)) -> dict:
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}
