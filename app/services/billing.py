"""Partner billing: we bill each client ourselves and switch their shop on or off.

The app on custom distribution cannot use Shopify's billing, so the client pays
us directly (Stripe, Razorpay, invoice). This is the backend side of that:

- an on/off switch per shop: off hides the chat launcher and answers any chat
  with a short "not available" line;
- an optional monthly chat limit: once a shop has had that many conversations
  this month, a new one gets a "limit reached" line, while conversations
  already started this month carry on;
- a note for us ("paid until 30 Nov").

Every shop is on, with no limit, until set otherwise - so nothing changes for a
shop nobody has touched. Kept on the shop's row under "_billing", which the
shop's own settings page and the storefront never see.
"""

from datetime import datetime

from sqlalchemy import distinct, func, or_, select

from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal
from app.services import installs, shops

KEY = "_billing"

PAUSED_MESSAGE = "The shopping assistant isn't available on this store right now."
LIMIT_MESSAGE = ("The shopping assistant has reached its chat limit for this month. "
                 "Please contact the store directly for help.")


def plan_for(shop: str) -> dict:
    saved = installs.settings_for(shop).get(KEY) or {}
    limit = saved.get("chat_limit")
    return {
        "active": saved.get("active", True) is not False,
        "chat_limit": int(limit) if isinstance(limit, int) and limit > 0 else None,
        "note": str(saved.get("note") or ""),
    }


def _month_start() -> datetime:
    now = datetime.utcnow()
    return datetime(now.year, now.month, 1)


def _this_shop(shop: str):
    # Rows saved before shops were told apart belong to the default shop.
    if shops.normalise(shop) == shops.default():
        return or_(ChatMessage.shop == shop, ChatMessage.shop.is_(None))
    return ChatMessage.shop == shop


async def chats_this_month(shop: str) -> int:
    """Conversations (distinct chats with at least one shopper message) this month."""
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(func.count(distinct(ChatMessage.session_id))).where(
                _this_shop(shop), ChatMessage.role == "user", ChatMessage.created_at >= _month_start())
        )).scalar_one()


async def _started_this_month(shop: str, session_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(ChatMessage.id).where(
                _this_shop(shop), ChatMessage.session_id == session_id, ChatMessage.role == "user",
                ChatMessage.created_at >= _month_start()).limit(1)
        )).first() is not None


async def refusal(shop: str, session_id: str) -> tuple[str, str] | None:
    """(code, shopper message) when this shop may not chat now, else None."""
    plan = plan_for(shop)
    if not plan["active"]:
        return "paused", PAUSED_MESSAGE
    if plan["chat_limit"] and not await _started_this_month(shop, session_id):
        if await chats_this_month(shop) >= plan["chat_limit"]:
            return "limit", LIMIT_MESSAGE
    return None


async def set_plan(shop: str, active: bool, chat_limit: int | None, note: str) -> dict:
    value = {"active": bool(active),
             "chat_limit": int(chat_limit) if chat_limit and int(chat_limit) > 0 else None,
             "note": (note or "")[:300]}
    await installs.save_private(shops.normalise(shop), KEY, value)
    return plan_for(shop)


async def overview() -> list[dict]:
    """Every shop we know, with its plan and this month's chats."""
    from app.core.config import settings
    names = {shops.default()} | installs.known() | {
        shops.normalise(d) for d in (settings.SUPPORT_SHOPS or "").split(",") if d.strip()}
    out = []
    for shop in sorted(n for n in names if n):
        out.append({"shop": shop, "installed": shop in installs.installed() or shop == shops.default(),
                    **plan_for(shop), "chats_this_month": await chats_this_month(shop)})
    return out
