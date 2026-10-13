"""How long shopper data is kept, and deleting it when asked.

Shoppers type emails, order numbers and addresses into the chat, so the
conversations are personal data even though they are kept under random ids.

- Conversations older than SUPPORT_CHAT_RETENTION_DAYS are deleted, once a day.
  An answer the owner reviewed stays (its verdict teaches later answers; the
  review page shows it), as do staff threads.
- Pending order changes hold an email and an address: they expire within
  minutes and are swept here after a day.
- Shopify's customers/redact webhook names a customer: every conversation in
  that shop that mentions their email is deleted, with their saved state.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from app.core.config import settings
from app.db.models import ChatMessage, KnowledgeChunk, OrderChangeRequest, ShopperState
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

SHOPPER_SESSIONS = "cs_%"      # support.SESSION_PREFIX
SWEEP_EVERY_HOURS = 24


async def purge_old() -> dict:
    """Delete what is past its keep-by date. Returns how much went."""
    gone = {"messages": 0, "order_changes": 0}
    async with AsyncSessionLocal() as db:
        stale = datetime.utcnow() - timedelta(days=1)
        result = await db.execute(delete(OrderChangeRequest).where(OrderChangeRequest.created_at < stale))
        gone["order_changes"] = result.rowcount or 0
        days = settings.SUPPORT_CHAT_RETENTION_DAYS
        if days > 0:
            reviewed = [int(r) for r in (await db.execute(
                select(KnowledgeChunk.ref_id).where(KnowledgeChunk.kind == "lesson"))).scalars().all()
                if r and str(r).isdigit()]
            query = delete(ChatMessage).where(ChatMessage.session_id.like(SHOPPER_SESSIONS),
                                              ChatMessage.created_at < datetime.utcnow() - timedelta(days=days))
            if reviewed:
                query = query.where(ChatMessage.id.notin_(reviewed))
            gone["messages"] = (await db.execute(query)).rowcount or 0
        await db.commit()
    return gone


async def keep_swept() -> None:
    """Run purge_old now and then daily, for as long as the server runs."""
    while True:
        try:
            gone = await purge_old()
            if any(gone.values()):
                logger.info("Retention: deleted %s", gone)
        except Exception:  # noqa: BLE001 - never take the server down
            logger.warning("Retention sweep failed", exc_info=True)
        await asyncio.sleep(SWEEP_EVERY_HOURS * 3600)


async def forget_customer(shop: str, email: str | None, customer_id: str | None) -> dict:
    """Shopify's customers/redact: delete this customer's conversations and state."""
    gone = {"conversations": 0, "state": 0}
    email = (email or "").strip().lower()
    async with AsyncSessionLocal() as db:
        if email and "@" in email:
            mentions = select(ChatMessage.session_id).where(
                ChatMessage.session_id.like(SHOPPER_SESSIONS),
                func.lower(ChatMessage.content).contains(email, autoescape=True))
            if shop:
                # Conversations saved before shops were recorded have no shop.
                mentions = mentions.where((ChatMessage.shop == shop) | (ChatMessage.shop.is_(None)))
            sessions = sorted(set((await db.execute(mentions)).scalars().all()))
            if sessions:
                await db.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(sessions)))
                await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.session_id.in_(sessions)))
                await db.execute(delete(OrderChangeRequest).where(OrderChangeRequest.session_id.in_(sessions)))
                gone["conversations"] = len(sessions)
            await db.execute(delete(OrderChangeRequest).where(func.lower(OrderChangeRequest.email) == email))
        if customer_id:
            result = await db.execute(delete(ShopperState).where(ShopperState.customer_key == f"customer:{customer_id}"))
            gone["state"] = result.rowcount or 0
        await db.commit()
    return gone


async def forget_shop(shop: str) -> dict:
    """Shopify's shop/redact (48 hours after uninstall): the shop's conversations
    and everything kept under its name."""
    from app.db.models import StoreSetting

    gone = {"messages": 0, "settings": 0}
    if not shop:
        return gone
    async with AsyncSessionLocal() as db:
        gone["messages"] = (await db.execute(delete(ChatMessage).where(ChatMessage.shop == shop))).rowcount or 0
        gone["settings"] = (await db.execute(
            delete(StoreSetting).where(StoreSetting.key.startswith(f"{shop}|", autoescape=True)))).rowcount or 0
        await db.commit()
    return gone
