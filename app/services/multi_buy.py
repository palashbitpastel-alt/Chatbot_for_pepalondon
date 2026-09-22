"""Multi-item discount: 10% off two pieces, 15% off three or more.

The discount itself lives in Shopify as automatic discounts, so checkout applies
it whatever the chat says. This module only does the arithmetic the storefront
shows beside a bag or a look - which tier the shopper is on, how many more
pieces reach the next one, and what they save - so the chat can say "add one
more for 15% off" and be right about it.

Tiers come from SUPPORT_MULTI_ITEM_TIERS ("2:10,3:15") and must match the
automatic discounts set up in the store. Empty turns the feature off.
"""

from decimal import ROUND_HALF_UP, Decimal

from app.core.config import settings

CENT = Decimal("0.01")


def tiers() -> list[dict]:
    """[{"min_items": 2, "percent": 10}, ...], smallest first. Bad entries are skipped."""
    parsed: dict[int, int] = {}
    for part in (settings.SUPPORT_MULTI_ITEM_TIERS or "").split(","):
        count, _, percent = part.strip().partition(":")
        if count.strip().isdigit() and percent.strip().isdigit():
            if int(count) >= 2 and 0 < int(percent) < 100:
                parsed[int(count)] = int(percent)
    return [{"min_items": c, "percent": parsed[c]} for c in sorted(parsed)]


def enabled() -> bool:
    return bool(tiers())


def _money(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(CENT)
    except Exception:  # noqa: BLE001 - a missing total just means no saving to show
        return Decimal("0.00")


def summary(item_count: int, subtotal, currency: str | None = None) -> dict | None:
    """Where this many items and this subtotal sit on the tiers.

    None when the feature is off. Money comes back as plain floats in the
    shop's currency, ready to print.
    """
    ladder = tiers()
    if not ladder:
        return None
    count = max(0, int(item_count or 0))
    total = _money(subtotal)

    current = None
    for tier in ladder:
        if count >= tier["min_items"]:
            current = tier
    upcoming = next((t for t in ladder if t["min_items"] > count), None)

    saving = (total * Decimal(current["percent"]) / 100).quantize(CENT, ROUND_HALF_UP) if current else Decimal("0")
    result = {
        "tiers": ladder,
        "item_count": count,
        "currency": currency,
        "subtotal": float(total),
        "current": current,
        "saving": float(saving),
        "total_after": float(total - saving),
        "next": None,
    }
    if upcoming:
        result["next"] = {**upcoming, "items_needed": upcoming["min_items"] - count}
    return result


def headline(info: dict | None) -> str | None:
    """One line for the agent's briefing, e.g. "2 items: 10% off applied; 1 more for 15% off"."""
    if not info:
        return None
    parts = []
    if info["current"]:
        parts.append(f"{info['current']['percent']}% multi-item saving applies")
    if info["next"]:
        n = info["next"]["items_needed"]
        parts.append(f"{n} more piece{'s' if n != 1 else ''} unlocks {info['next']['percent']}% off")
    return f"{info['item_count']} item(s): " + "; ".join(parts) if parts else None
