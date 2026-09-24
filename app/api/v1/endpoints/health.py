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


# Scope gaps have cost this project several rounds of guesswork: a tool answers
# "I can't see our delivery options" whether the token lacks read_shipping, the
# query is wrong, or the merchant never set up shipping. Asking Shopify which
# scopes the token actually holds settles it in one request.
@router.get("/health/shopify")
async def shopify_health_check() -> dict:
    from app.services.shopify_storefront import ShopifyError, graphql

    try:
        data = await graphql("{ currentAppInstallation { accessScopes { handle } } }")
    except (ShopifyError, KeyError, ValueError) as exc:
        return {"status": "error", "detail": str(exc)[:200]}
    granted = sorted(s["handle"] for s in
                     (data.get("currentAppInstallation") or {}).get("accessScopes") or [])
    wanted = ["read_products", "read_orders", "read_customers", "read_inventory",
              "read_discounts", "read_shipping", "read_metaobjects", "write_orders"]
    return {"status": "ok", "granted": granted,
            "missing": [s for s in wanted if s not in granted]}
