from fastapi import APIRouter

from app.core.config import settings
from app.services import insights
from app.services.shopify_store import fetch_store_snapshot, is_configured

router = APIRouter(tags=["shopify"])


@router.get("/shopify/status")
async def shopify_status() -> dict:
    """Connection state plus what the store's live data says (currency, granted
    scopes, where campaign data comes from). Nothing here is cached - it's a
    fresh read on every call, same as the rest of the dashboard."""
    snapshot = await fetch_store_snapshot()
    return {
        "configured": is_configured(),
        "store_url": settings.SHOPIFY_STORE_URL or None,
        "api_version": settings.SHOPIFY_API_VERSION,
        **insights.store_meta(snapshot),
    }
