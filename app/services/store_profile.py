"""What the shop is, in the words a shopper hears.

The agent is told nothing about the store it works for unless something passes
it along. A hello and "what do you sell?" need the same answer - the name, what
we sell, the main categories - so it is built once, here.
"""

import logging

from app.core.config import settings
from app.services import shopify_storefront
from app.services.suggestions import plural

logger = logging.getLogger(__name__)

OVERVIEW_CATEGORIES = 8     # read this many, then fold "Dress"/"Dresses" together


def _singular(name: str) -> str:
    key = name.strip().lower()
    if key.endswith("es") and key[:-2].endswith("ss"):
        return key[:-2]
    if key.endswith("s") and not key.endswith("ss"):
        return key[:-1]
    return key


async def store_name() -> str:
    """What the shop is called: the configured brand, else Shopify's own name."""
    configured = settings.SUPPORT_STORE_NAME.strip()
    if configured:
        return configured
    return (await shopify_storefront.shop_info())["name"]


async def main_categories(limit: int = 6) -> list[str]:
    """The fullest categories, already plural, with "Dress"/"Dresses" twins folded
    together - they are separate product types but one shelf to a shopper."""
    listed = (await shopify_storefront.categories(OVERVIEW_CATEGORIES))["categories"]
    names: dict[str, str] = {}
    for entry in listed:
        names.setdefault(_singular(entry["name"]), plural(entry["name"]))
    return list(names.values())[:limit]


async def overview(limit: int = 6) -> dict:
    """Name, what we sell and the main categories.

    Deliberately no product count: a raw "50 products" makes a curated range
    sound small, and it is out of date the moment the catalogue changes. Each
    part is optional, so a slow lookup costs a detail rather than the answer.
    """
    about: dict = {"what_we_sell": settings.SUPPORT_STORE_DESCRIPTION}
    try:
        about["name"] = await store_name()
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the store name", exc_info=True)
    try:
        about["categories"] = await main_categories(limit)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the main categories", exc_info=True)
    return about


# ── What the shop actually holds ────────────────────────────────────────────
# The agent used to work blind: it could look a product up, but it had no sense
# of the shop around it - what is sold, for which ages, at what prices, for what
# occasions. So it guessed, and a guess in front of a shopper reads as stupid.
# This is that sense, built from the catalogue itself and refreshed as it
# changes, small enough to ride along with every single turn.

_FACTS_CACHE: tuple[float, str] | None = None
FACTS_SECONDS = 900


def _size_span(products: list[dict]) -> str:
    """"1M - 12Y", read off the sizes the store actually sells."""
    from app.services import size_finder

    seen = [s for p in products for s in (p.get("sizes") or [])]
    ordered = [b.label for b in size_finder.CHART]
    have = [label for label in ordered if any(label.lower() == s.strip().lower() for s in seen)]
    return f"{have[0]} - {have[-1]}" if len(have) > 1 else (have[0] if have else "")


async def facts() -> str:
    """A short, true description of this shop, for the agent to answer from.

    Everything here is read from the store: nothing is written into the code, so
    a shop that starts selling coats says so the next time this refreshes.
    """
    global _FACTS_CACHE
    import time

    from app.services import occasions, outfit

    now = time.monotonic()
    if _FACTS_CACHE and now - _FACTS_CACHE[0] < FACTS_SECONDS:
        return _FACTS_CACHE[1]

    catalogue = await outfit.browse_catalogue()
    products = [p for p in catalogue["products"] if p.get("in_stock")]
    lines = ["[This shop - answer from these facts, and look up anything they do not cover]"]
    try:
        lines.append(f"Name: {await store_name()}")
    except Exception:  # noqa: BLE001 - a fact sheet must not fail on one lookup
        logger.debug("No store name for the fact sheet", exc_info=True)
    if settings.SUPPORT_STORE_DESCRIPTION:
        lines.append(f"Sells: {settings.SUPPORT_STORE_DESCRIPTION}")

    counted: dict[str, int] = {}
    for product in products:
        if product.get("category"):
            counted[product["category"]] = counted.get(product["category"], 0) + 1
    if counted:
        top = sorted(counted.items(), key=lambda kv: -kv[1])[:8]
        lines.append("In stock now: " + ", ".join(f"{name} ({count})" for name, count in top))

    prices = [p["price_from"] for p in products if p.get("price_from")]
    if prices:
        currency = catalogue.get("currency") or ""
        lines.append(f"Prices run from {min(prices):g} to {max(prices):g} {currency}"
                     " (the shopper is quoted their own market's price)")
    span = _size_span(products)
    if span:
        lines.append(f"Sizes: {span}")

    worn = {label for p in products for label in (p.get("occasions") or [])}
    if worn:
        lines.append("Occasions the range covers: "
                     + ", ".join(label for label, _ in occasions.GROUPS if label in worn))
        lines.append("Anything else - a ski suit, school uniform - we do not stock: say so plainly.")

    text = "\n".join(lines)
    _FACTS_CACHE = (now, text)
    return text


# ── The shop's own published policies ───────────────────────────────────────
# "Do you ship to India?" and "what is your returns policy?" are two of the
# three things shoppers ask, and the answer was "our handbook does not say" -
# while the real policy sat published on the storefront the whole time. These
# are the merchant's own words, read from Shopify, never summarised into rules.

POLICIES = """
query ShopPolicies {
  shop {
    shopPolicies { type title body url }
  }
}
"""
_POLICY_CACHE: tuple[float, list[dict]] | None = None
POLICY_SECONDS = 900
POLICY_CHARS = 900


def _plain(html: str | None) -> str:
    """Policy bodies come as HTML; the agent reads prose."""
    import re

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    return " ".join(text.split())


async def policies() -> list[dict]:
    """Every policy the merchant has published, as plain text. Empty if none are."""
    global _POLICY_CACHE
    import time

    from app.services.shopify_client import graphql

    now = time.monotonic()
    if _POLICY_CACHE and now - _POLICY_CACHE[0] < POLICY_SECONDS:
        return _POLICY_CACHE[1]
    data = await graphql(POLICIES)
    found = []
    for policy in ((data.get("shop") or {}).get("shopPolicies") or []):
        body = _plain(policy.get("body"))
        if not body:
            continue
        found.append({
            "policy": policy.get("title") or policy.get("type"),
            "text": body[:POLICY_CHARS] + ("…" if len(body) > POLICY_CHARS else ""),
            "url": policy.get("url"),
        })
    _POLICY_CACHE = (now, found)
    return found
