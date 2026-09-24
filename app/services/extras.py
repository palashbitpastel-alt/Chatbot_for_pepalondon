"""The assistant's everyday extras: product questions, discount codes, delivery
dates and the wishlist.

Each one reads the store rather than trusting the model: a product's care line
comes from its own description, a code is checked against the store's real
discounts, and delivery dates are worked out in code from the dispatch cut-off
and each method's transit days - the model only ever relays the answer.
"""

import datetime as dt
import html
import re
import time
from zoneinfo import ZoneInfo

from app.services import compare
from app.services.shopify_client import ShopifyError, graphql
from app.services.shopify_storefront import _details_from, collection_tree, shop_info

# ── Product questions ─────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_CARE_RE = re.compile(r"\b(wash|machine|hand wash|dry clean|tumble|iron|bleach|care|delicate|cold)\b", re.I)


def _text(description_html: str | None, limit: int = 1500) -> str:
    """The description as plain sentences, list items on their own lines."""
    raw = re.sub(r"</(p|li|h\d|div)>|<br\s*/?>", "\n", description_html or "", flags=re.I)
    text = html.unescape(_TAG_RE.sub("", raw))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out = "\n".join(line for line in lines if line)
    return out[:limit]


async def _resolve(name: str) -> tuple[str | None, list[str]]:
    tree = await collection_tree()
    return compare.resolve(name, tree["ids"], tree["titles"])


async def product_details(name: str) -> dict:
    """Everything the store says about one product, for "is it machine washable?"."""
    product_id, near = await _resolve(name)
    if product_id is None:
        return {"found": False, "asked_for": name, "did_you_mean": near}
    data = await graphql(compare.COMPARE_DETAILS, {"ids": [f"gid://shopify/Product/{product_id}"]})
    node = (data.get("nodes") or [None])[0]
    if not node or node.get("status") != "ACTIVE":
        return {"found": False, "asked_for": name, "did_you_mean": near}
    currency = (await shop_info())["currency"]
    facts = compare._facts(node, currency)
    description = _text(node.get("descriptionHtml"))
    care = [line for line in description.splitlines() if _CARE_RE.search(line)]
    in_stock = [v for v in (node.get("variants") or {}).get("nodes") or [] if v.get("availableForSale")]
    return {
        "found": True,
        **{k: facts[k] for k in ("product_id", "variant_id", "title", "handle", "price_from", "price_to",
                                 "currency", "image", "url", "for", "colours", "size_range", "fabric",
                                 "made_in", "highlights")},
        "category": facts.get("category"),
        "description": description,
        "care": care,
        "tags": [t for t in node.get("tags") or [] if t.lower() not in ("all products", "in-stock", "top products")],
        "in_stock_variants": len(in_stock),
        # What the merchant recorded in the product's own fields - fabric, age
        # group, sleeve length, care. The question this tool exists for ("is it
        # machine washable?") is usually answered here rather than in the prose.
        "details": _details_from(node) or None,
        "in_collections": [c["title"] for c in ((node.get("collections") or {}).get("nodes") or [])
                           if c and c.get("title")][:5],
        "note": "Answer from description, highlights, care and details - details holds the fields the "
                "merchant filled in, and is often where the answer is. If it is in none of them, say "
                "the product page does not say, and offer our team.",
    }


# ── Discount codes ────────────────────────────────────────────────────────

CODE_LOOKUP = """
query SupportDiscountCode($code: String!) {
  codeDiscountNodeByCode(code: $code) {
    codeDiscount {
      __typename
      ... on DiscountCodeBasic { title status summary endsAt }
      ... on DiscountCodeBxgy { title status summary endsAt }
      ... on DiscountCodeFreeShipping { title status summary endsAt }
      ... on DiscountCodeApp { title status endsAt }
    }
  }
}
"""


async def discount_code(code: str) -> dict:
    """Whether a code exists and is live. Checkout still decides if the bag qualifies."""
    code = (code or "").strip()
    if not code or len(code) > 60:
        return {"valid": False, "reason": "no_code"}
    data = await graphql(CODE_LOOKUP, {"code": code})
    node = (data.get("codeDiscountNodeByCode") or {}).get("codeDiscount")
    if not node:
        return {"valid": False, "code": code, "reason": "not_found"}
    status = node.get("status")
    if status != "ACTIVE":
        return {"valid": False, "code": code, "reason": "expired" if status == "EXPIRED" else "not_active"}
    return {
        "valid": True,
        "code": code,
        "summary": node.get("summary") or node.get("title"),
        "ends": (node.get("endsAt") or "")[:10] or None,
        "action": {"type": "apply_discount", "code": code},
    }


# ── Delivery dates ────────────────────────────────────────────────────────
# Read from the store's own shipping settings - zones, the countries in each,
# and every method's name, price and description - never from a setting of ours.
# A method's delivery time is whatever it states ("Standard (3-5 business days)",
# "Next day"); one that states none is offered without dates rather than with
# invented ones.

SHIPPING = """
query SupportShipping {
  shop { ianaTimezone billingAddress { countryCodeV2 } }
  deliveryProfiles(first: 10) {
    nodes {
      profileLocationGroups {
        locationGroupZones(first: 50) {
          nodes {
            zone { name countries { code { countryCode restOfWorld } } }
            methodDefinitions(first: 25) {
              nodes {
                name
                description
                active
                rateProvider { ... on DeliveryRateDefinition { price { amount currencyCode } } }
              }
            }
          }
        }
      }
    }
  }
}
"""
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAYS_RE = re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*(?:business\s+|working\s+)?days?"
                      r"|(\d{1,2})\s*(?:business\s+|working\s+)?days?", re.I)
_shipping_cache: tuple[float, dict] | None = None
SHIPPING_CACHE_SECONDS = 600


def _transit(text: str) -> tuple[int, int] | None:
    """(min, max) business days a method states in its name or description."""
    if re.search(r"\bnext[\s-]day\b", text or "", re.I):
        return 1, 1
    m = _DAYS_RE.search(text or "")
    if not m:
        return None
    if m.group(1):
        return int(m.group(1)), int(m.group(2))
    return int(m.group(3)), int(m.group(3))


async def _shipping() -> dict:
    global _shipping_cache
    if _shipping_cache and time.monotonic() - _shipping_cache[0] < SHIPPING_CACHE_SECONDS:
        return _shipping_cache[1]
    data = await graphql(SHIPPING)
    methods = []
    for profile in data["deliveryProfiles"]["nodes"]:
        for group in profile.get("profileLocationGroups") or []:
            for zone in (group.get("locationGroupZones") or {}).get("nodes") or []:
                codes, rest_of_world = [], False
                for c in (zone.get("zone") or {}).get("countries") or []:
                    code = c.get("code") or {}
                    if code.get("restOfWorld"):
                        rest_of_world = True
                    elif code.get("countryCode"):
                        codes.append(code["countryCode"])
                for m in (zone.get("methodDefinitions") or {}).get("nodes") or []:
                    if not m.get("active"):
                        continue
                    price = ((m.get("rateProvider") or {}).get("price") or {})
                    methods.append({
                        "name": m.get("name"),
                        "zone": (zone.get("zone") or {}).get("name"),
                        "countries": codes,
                        "rest_of_world": rest_of_world,
                        "price": price.get("amount"),
                        "currency": price.get("currencyCode"),
                        "transit": _transit(f"{m.get('name') or ''} {m.get('description') or ''}"),
                    })
    shop = data.get("shop") or {}
    result = {
        "timezone": shop.get("ianaTimezone") or "UTC",
        "home_country": (shop.get("billingAddress") or {}).get("countryCodeV2"),
        "methods": methods,
    }
    _shipping_cache = (time.monotonic(), result)
    return result


def _add_business_days(day: dt.date, n: int) -> dt.date:
    while n > 0:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def _target(by: str | None, today: dt.date) -> dt.date | None:
    """"saturday", "2026-09-26", "26/09" -> the next such date."""
    if not by:
        return None
    text = by.strip().lower()
    words = re.findall(r"[a-z]+", text)
    for i, name in enumerate(_WEEKDAYS):
        if any(len(w) >= 3 and name.startswith(w) for w in words):
            ahead = (i - today.weekday()) % 7 or 7
            return today + dt.timedelta(days=ahead)
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text):
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})", text):
        guess = dt.date(today.year, int(m.group(2)), int(m.group(1)))
        return guess if guess >= today else guess.replace(year=today.year + 1)
    return None


async def delivery_estimate(country: str = "", by: str = "") -> dict:
    """The store's shipping methods for a country, with dates where a method states its time."""
    try:
        setup = await _shipping()
    except (ShopifyError, KeyError) as exc:
        return {"available": False, "reason": str(exc)[:120],
                "tell_customer": "I can't see our delivery options just now - the shipping policy has them, "
                                 "or our team can confirm."}
    code = (country or setup["home_country"] or "").strip().upper()
    fits = [m for m in setup["methods"] if code in m["countries"]] or \
           [m for m in setup["methods"] if m["rest_of_world"]]
    if not fits:
        return {"available": True, "ships_there": False, "country": code}
    try:
        today = dt.datetime.now(ZoneInfo(setup["timezone"])).date()
    except Exception:  # noqa: BLE001 - an unknown zone: the server's own date
        today = dt.date.today()
    target = _target(by, today)
    options = []
    for m in fits:
        option = {"method": m["name"], "price": m["price"], "currency": m["currency"]}
        if m["transit"]:
            low, high = m["transit"]
            earliest, latest = _add_business_days(today, low), _add_business_days(today, high)
            option["business_days"] = f"{low}-{high}" if high != low else str(low)
            option["arrives_between"] = [earliest.strftime("%a %d %b"), latest.strftime("%a %d %b")]
            if target:
                option["by_target"] = "yes" if latest <= target else ("maybe" if earliest <= target else "no")
        else:
            option["delivery_time"] = "not stated by the store"
        options.append(option)
    return {
        "available": True,
        "country": code or None,
        "today": today.strftime("%a %d %b"),
        "target": target.strftime("%a %d %b") if target else None,
        "options": options,
        "note": "Dates count business days from today and are estimates. A method with no stated time: "
                "give its name and price only, never a date.",
    }


# ── Wishlist ──────────────────────────────────────────────────────────────

async def product_card(name: str) -> dict:
    """A product the shopper named, as a card the widget can save."""
    product_id, near = await _resolve(name)
    if product_id is None:
        return {"found": False, "asked_for": name, "did_you_mean": near}
    data = await graphql(compare.COMPARE_DETAILS, {"ids": [f"gid://shopify/Product/{product_id}"]})
    node = (data.get("nodes") or [None])[0]
    if not node or node.get("status") != "ACTIVE":
        return {"found": False, "asked_for": name, "did_you_mean": near}
    facts = compare._facts(node, (await shop_info())["currency"])
    return {"found": True, **{k: facts[k] for k in ("product_id", "variant_id", "title", "image", "url",
                                                   "price_from", "currency")}}
