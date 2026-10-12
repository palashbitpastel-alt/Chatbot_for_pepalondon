"""Partner billing: we bill each client ourselves and switch their shop on or off.

The app on custom distribution cannot use Shopify's billing, so the client pays
us directly (Stripe, Razorpay, invoice). This is the backend side of that:

- an on/off switch per shop: off hides the chat launcher and answers any chat
  with a short "not available" line;
- an optional monthly chat limit: once a shop has had that many conversations
  this month, a new one gets a "limit reached" line, while conversations
  already started this month carry on;
- a note for us ("paid until 30 Nov");
- a paid-until date: once it has passed by SUPPORT_GRACE_DAYS, the shop
  pauses itself (same as off) until it renews. A shop with no date never
  pauses this way.

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
MERCHANT_PAUSE_KEY = "_merchant_paused"

# The plans merchants see on the app's page. SUPPORT_PLANS (a JSON list in the
# same shape) replaces these without a code change. payment_url: a Stripe or
# Razorpay payment link for the plan, used by its Upgrade button.
DEFAULT_PLANS = [
    {"key": "starter", "name": "Starter", "price": "$29 / month", "monthly": 29, "chats": 1000,
     "features": ["1,000 chats a month", "Product search and recommendations", "Size finder", "Email support"]},
    {"key": "growth", "name": "Growth", "price": "$79 / month", "monthly": 79, "chats": 5000,
     "features": ["5,000 chats a month", "Everything in Starter", "Complete-the-look outfits", "Priority support"]},
    {"key": "pro", "name": "Pro", "price": "$199 / month", "monthly": 199, "chats": 15000,
     "features": ["15,000 chats a month", "Everything in Growth", "Custom assistant instructions", "Dedicated support"]},
]


# How long a merchant can pay for at once, and the saving for each (percent).
# SUPPORT_PERIOD_DISCOUNTS ({"1": 0, "3": 5, "6": 10, "12": 20}) replaces these.
DEFAULT_DISCOUNTS = {1: 0, 3: 5, 6: 10, 12: 20}


def discounts() -> dict[int, int]:
    from app.core.config import settings
    import json
    try:
        custom = json.loads(settings.SUPPORT_PERIOD_DISCOUNTS or "null")
        if isinstance(custom, dict) and custom:
            return {int(k): max(0, min(90, int(v))) for k, v in custom.items() if int(k) in (1, 3, 6, 12)}
    except (ValueError, TypeError):
        pass
    return dict(DEFAULT_DISCOUNTS)


def quote(plan: dict, months: int) -> dict:
    """What a plan costs for a period: total, per month, the saving."""
    monthly = float(plan.get("monthly") or 0)
    off = discounts().get(months, 0)
    total = round(monthly * months * (100 - off) / 100, 2)
    return {"months": months, "discount": off, "total": total,
            "per_month": round(total / months, 2) if months else total}


def plans() -> list[dict]:
    from app.core.config import settings
    import json
    try:
        custom = json.loads(settings.SUPPORT_PLANS or "null")
        if isinstance(custom, list) and all(isinstance(p, dict) and p.get("key") for p in custom):
            return custom
    except ValueError:
        pass
    return DEFAULT_PLANS


def plan_named(key: str | None) -> dict | None:
    return next((p for p in plans() if p.get("key") == key), None)

PAUSED_MESSAGE = "The shopping assistant isn't available on this store right now."
LIMIT_MESSAGE = ("The shopping assistant has reached its chat limit for this month. "
                 "Please contact the store directly for help.")


def plan_for(shop: str) -> dict:
    saved = installs.settings_for(shop).get(KEY) or {}
    limit = saved.get("chat_limit")
    chosen = plan_named(saved.get("plan"))
    if not (isinstance(limit, int) and limit > 0):
        limit = chosen.get("chats") if chosen else None   # the plan's own allowance
    active = saved.get("active", True) is not False
    paid_until = str(saved.get("paid_until") or "")
    grace_until, lapsed = "", False
    if paid_until:
        from datetime import date, timedelta
        from app.core.config import settings
        try:
            grace = date.fromisoformat(paid_until) + timedelta(days=max(0, settings.SUPPORT_GRACE_DAYS))
            grace_until, lapsed = grace.isoformat(), date.today() > grace
        except ValueError:
            pass
    return {
        "active": active,                    # our on/off switch
        "lapsed": lapsed,                    # paid time and grace days are over
        # The merchant's own pause switch in the app (kept apart from billing).
        "merchant_paused": bool(installs.settings_for(shop).get(MERCHANT_PAUSE_KEY)),
        "running": active and not lapsed and not installs.settings_for(shop).get(MERCHANT_PAUSE_KEY),
        "grace_until": grace_until,
        "plan": chosen["key"] if chosen else None,
        "chat_limit": int(limit) if isinstance(limit, int) and limit > 0 else None,
        "paid_until": str(saved.get("paid_until") or ""),
        "paid_from": str(saved.get("paid_from") or ""),
        "payment_url": str(saved.get("payment_url") or ""),
        "note": str(saved.get("note") or ""),
        "requested": saved.get("requested") if isinstance(saved.get("requested"), dict) else None,
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
    if not plan["running"]:
        if plan["merchant_paused"] and plan["active"] and not plan["lapsed"]:
            return "merchant_paused", PAUSED_MESSAGE
        return ("expired" if plan["active"] else "paused"), PAUSED_MESSAGE
    if plan["chat_limit"] and not await _started_this_month(shop, session_id):
        if await chats_this_month(shop) >= plan["chat_limit"]:
            return "limit", LIMIT_MESSAGE
    return None


def _paid_from(shop: str, paid_until: str) -> str:
    """Keep the period's start when the paid-until date is unchanged; a new
    date set by hand starts the period today."""
    from datetime import date
    saved = installs.settings_for(shop).get(KEY) or {}
    if paid_until and paid_until == saved.get("paid_until"):
        return str(saved.get("paid_from") or "")
    return date.today().isoformat() if paid_until else ""


async def set_plan(shop: str, active: bool, chat_limit: int | None, note: str, plan: str | None = None,
                   paid_until: str = "", payment_url: str = "") -> dict:
    import re
    value = {"active": bool(active),
             "plan": plan if plan_named(plan) else None,
             # Empty: the plan's own allowance. A number: a limit set for this shop.
             "chat_limit": int(chat_limit) if chat_limit and int(chat_limit) > 0 else None,
             "paid_until": paid_until if re.fullmatch(r"\d{4}-\d{2}-\d{2}", paid_until or "") else "",
             "paid_from": _paid_from(shop, paid_until),
             "payment_url": payment_url if (payment_url or "").startswith("https://") else "",
             "note": (note or "")[:300],
             # Saving the store's plan answers the merchant's request.
             "requested": None}
    await installs.save_private(shops.normalise(shop), KEY, value)
    return plan_for(shop)


async def overview() -> list[dict]:
    """Every shop we know, with its plan and this month's chats."""
    from app.core.config import settings
    names = {shops.default()} | installs.known() | {
        shops.normalise(d) for d in (settings.SUPPORT_SHOPS or "").split(",") if d.strip()}
    out = []
    for shop in sorted(n for n in names if n):
        custom = (installs.settings_for(shop).get(KEY) or {}).get("chat_limit")
        out.append({"shop": shop, "installed": shop in installs.installed() or shop == shops.default(),
                    **plan_for(shop), "custom_limit": custom if isinstance(custom, int) and custom > 0 else None,
                    "chats_this_month": await chats_this_month(shop),
                    "subscription": {k: v for k, v in (installs.settings_for(shop).get("_stripe") or {}).items()
                                     if k in ("status", "cancel_at_period_end", "period_end", "months")} or None})
    return out


async def for_merchant(shop: str) -> dict:
    """What the merchant sees on the app's page: their plan, usage and how to
    pay - never our private note."""
    from datetime import date
    plan = plan_for(shop)
    used = await chats_this_month(shop)
    limit = plan["chat_limit"]
    chosen = plan_named(plan["plan"])
    days_left = days_total = None
    if plan["paid_until"]:
        try:
            end = date.fromisoformat(plan["paid_until"])
            days_left = (end - date.today()).days
            if plan["paid_from"]:
                days_total = max(1, (end - date.fromisoformat(plan["paid_from"])).days)
        except ValueError:
            days_left = days_total = None
    return {
        "active": plan["active"],
        "lapsed": plan["lapsed"],
        "merchant_paused": plan["merchant_paused"],
        "running": plan["running"],
        "grace_until": plan["grace_until"],
        "plan": chosen,
        "chats_used": used,
        "chat_limit": limit,
        "percent": min(100, round(used * 100 / limit)) if limit else None,
        "paid_until": plan["paid_until"],
        "days_left": days_left,
        "days_total": days_total,
        "payment_url": plan["payment_url"] or (chosen or {}).get("payment_url") or "",
        "requested": plan["requested"],
        "plans": plans(),
        "periods": [{"months": m, "discount": d} for m, d in sorted(discounts().items())],
    }


async def request_plan(shop: str, key: str, months: int = 1) -> dict | None:
    """The merchant picked a plan in the app: remember it for us to confirm
    (we switch them once they have paid). None for an unknown plan."""
    from datetime import date
    chosen = plan_named(key)
    if not chosen:
        return None
    months = months if months in discounts() else 1
    saved = dict(installs.settings_for(shop).get(KEY) or {})
    saved["requested"] = {"plan": key, "months": months, "total": quote(chosen, months)["total"],
                          "on": date.today().isoformat()}
    await installs.save_private(shops.normalise(shop), KEY, saved)
    return saved["requested"]


def _add_months(start, months: int):
    from datetime import date
    month = start.month - 1 + max(1, months)
    year, month = start.year + month // 12, month % 12 + 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30,
            31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(start.day, last))


def _per_day(plan: dict, months: int) -> float:
    return quote(plan, months)["per_month"] / 30


def change_quote(shop: str, key: str, months: int) -> dict:
    """What buying `key` for `months` does to this shop, priced and dated:

    renew  - same plan with time left: the months go on after the current end.
    change - another plan with time left: it starts today; the unused time's
             value (at the price of the old plan's period) becomes extra days
             of the new plan at its own price. Time left can't be multiplied.
    new    - no time left: starts today.
    """
    from datetime import date
    chosen = plan_named(key)
    months = months if months in discounts() else 1
    q = quote(chosen, months) if chosen else {"months": months, "discount": 0, "total": 0, "per_month": 0}
    today = date.today()
    current = plan_for(shop)
    current_plan = plan_named(current["plan"])
    days_left = 0
    try:
        end = date.fromisoformat(current["paid_until"])
        days_left = max(0, (end - today).days)
    except ValueError:
        end = None
    if not (current["active"] and current_plan and end and days_left > 0):
        return {**q, "kind": "new", "ends": _add_months(today, months).isoformat()}
    if current_plan["key"] == key:
        return {**q, "kind": "renew", "starts": end.isoformat(), "ends": _add_months(end, months).isoformat()}
    # The old plan's period, as near as the dates tell (30-day months).
    try:
        span = (end - date.fromisoformat(current["paid_from"])).days
    except ValueError:
        span = 30
    old_months = min(discounts(), key=lambda m: abs(m * 30 - span))
    credit = round(days_left * _per_day(current_plan, old_months), 2) if current_plan.get("monthly") else 0
    per_day = _per_day(chosen, months) if chosen and chosen.get("monthly") else 0
    extra = int(credit // per_day) if per_day else 0
    from datetime import timedelta
    return {**q, "kind": "change", "credit": credit, "days_left": days_left, "from_plan": current_plan["name"],
            "extra_days": extra, "ends": (_add_months(today, months) + timedelta(days=extra)).isoformat()}


async def apply_paid_plan(shop: str, key: str, months: int) -> dict:
    """Put a paid-for plan in place, dated by change_quote."""
    from datetime import date
    shop = shops.normalise(shop)
    change = change_quote(shop, key, months)
    saved = dict(installs.settings_for(shop).get(KEY) or {})
    saved.update({"plan": key, "active": True, "chat_limit": None, "requested": None,
                  "paid_from": saved.get("paid_from") if change["kind"] == "renew" and saved.get("paid_from") else date.today().isoformat(),
                  "paid_until": change["ends"]})
    await installs.save_private(shop, KEY, saved)
    return plan_for(shop)


async def answer_request(shop: str, accept: bool, months: int | None = None) -> dict:
    """Accept: the shop gets the plan it asked for, dated by change_quote.
    Decline: the request is dropped and nothing else changes."""
    shop = shops.normalise(shop)
    saved = dict(installs.settings_for(shop).get(KEY) or {})
    asked = (saved.get("requested") or {}).get("plan")
    months = months or int((saved.get("requested") or {}).get("months") or 1)
    if accept and plan_named(asked):
        return await apply_paid_plan(shop, asked, months)
    saved["requested"] = None
    await installs.save_private(shop, KEY, saved)
    return plan_for(shop)
