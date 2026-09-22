"""Multi-item discount: e.g. 10% off two pieces, 15% off three or more.

The discount is the merchant's own: automatic discounts set up in Shopify, which
checkout applies whatever the chat says. This module reads those discounts and
does the arithmetic the storefront shows beside a bag or a look - which tier the
shopper is on, how many more pieces reach the next one, and what they save - so
the chat can say "add one more for 15% off" and be right about it.

A tier is any ACTIVE automatic basic discount with a minimum item quantity and a
percentage off. Change or end them in Shopify admin and the chat follows within
TIER_CACHE_SECONDS. None set up means no offer is shown.
"""

import logging
import time
from decimal import ROUND_HALF_UP, Decimal

from app.services.shopify_client import ShopifyError, graphql

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
TIER_CACHE_SECONDS = 600

TIERS_QUERY = """
query MultiItemTiers {
  automaticDiscountNodes(first: 50, query: "status:active") {
    nodes {
      automaticDiscount {
        __typename
        ... on DiscountAutomaticBasic {
          status
          minimumRequirement { __typename ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity } }
          customerGets { value { __typename ... on DiscountPercentage { percentage } } }
        }
      }
    }
  }
}
"""

_cache: tuple[float, list[dict]] | None = None


def _parse(nodes: list[dict]) -> list[dict]:
    """[{"min_items": 2, "percent": 10}, ...] from the store's automatic discounts."""
    best: dict[int, int] = {}
    for node in nodes:
        d = node.get("automaticDiscount") or {}
        if d.get("__typename") != "DiscountAutomaticBasic" or d.get("status") != "ACTIVE":
            continue
        qty = ((d.get("minimumRequirement") or {}).get("greaterThanOrEqualToQuantity"))
        pct = (((d.get("customerGets") or {}).get("value") or {}).get("percentage"))
        try:
            count, percent = int(qty), round(float(pct) * 100)
        except (TypeError, ValueError):
            continue
        if count >= 2 and 0 < percent < 100:
            best[count] = max(percent, best.get(count, 0))
    return [{"min_items": c, "percent": best[c]} for c in sorted(best)]


async def tiers() -> list[dict]:
    """The store's multi-item tiers, smallest first. [] when none are set up or
    the store cannot be read - the chat then simply shows no offer."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < TIER_CACHE_SECONDS:
        return _cache[1]
    try:
        data = await graphql(TIERS_QUERY)
        ladder = _parse(data["automaticDiscountNodes"]["nodes"])
    except (ShopifyError, KeyError) as exc:
        logger.warning("Could not read the store's automatic discounts: %s", exc)
        ladder = _cache[1] if _cache else []
    _cache = (time.monotonic(), ladder)
    return ladder


def _money(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(CENT)
    except Exception:  # noqa: BLE001 - a missing total just means no saving to show
        return Decimal("0.00")


def summary(ladder: list[dict], item_count: int, subtotal, currency: str | None = None) -> dict | None:
    """Where this many items and this subtotal sit on the tiers (from ``tiers()``).

    None when the store has no tiers. Money comes back as plain floats in the
    shop's currency, ready to print.
    """
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
