"""Outfit building for the customer support agent.

The agent decides *what* goes together - that is a judgement about occasion, age
and style. This module owns everything the agent must not be trusted to do
itself: resolving a choice to a real purchasable variant, checking it is in
stock, adding the money up exactly, comparing it to the shopper's budget, and
naming the exact variant ids. Adding the look to the bag is the frontend's job -
it gets ``cart_items`` and takes it from there.

Reads are live from the Shopify Admin API and restricted to ACTIVE products, so
a look can never contain something a shopper cannot buy.
"""

import json
import logging
from decimal import Decimal, InvalidOperation

from app.services.shopify_client import ShopifyError, graphql
from app.services.shopify_storefront import (
    product_image,
    product_url,
    shop_info,
    variant_image,
)

logger = logging.getLogger(__name__)

MAX_PRODUCTS = 50
MAX_VARIANTS = 100
MAX_OUTFIT_ITEMS = 8

OUTFIT_FORMAT = (
    '[{"handle": "product-handle", "color": "Pink", "size": "5Y", "quantity": 1}] '
    "- color and size may be omitted for products that do not have them"
)

# productType is authoritative when the merchant sets it. Most of this store's
# products leave it blank, so fall back to reading the title.
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Shoes", ("shoe", "boot", "sandal", "trainer", "sneaker", "pump", "loafer")),
    ("Dress", ("dress", "pinafore", "romper", "gown")),
    ("Top", ("shirt", "blouse", "top", "jumper", "cardigan", "sweater", "sweatshirt", "tee")),
    ("Bottoms", ("trouser", "short", "skirt", "legging", "jean", "dungaree")),
    ("Outerwear", ("coat", "jacket", "gilet")),
    ("Accessory", ("hairband", "headband", "bow", "hat", "cap", "sock", "tight",
                   "bag", "belt", "scarf", "clip", "bib")),
]

CATALOGUE = """
query OutfitCatalogue($query: String!, $first: Int!, $variants: Int!) {
  products(first: $first, query: $query, sortKey: TITLE) {
    nodes {
      legacyResourceId
      title
      handle
      productType
      tags
      onlineStoreUrl
      description(truncateAt: 240)
      featuredMedia { ... on MediaImage { image { url altText } } }
      options { name values }
      variants(first: $variants) {
        nodes {
          legacyResourceId
          sku
          title
          price
          availableForSale
          inventoryQuantity
          selectedOptions { name value }
          media(first: 1) { nodes { ... on MediaImage { image { url } } } }
        }
      }
    }
  }
}
"""


def _category(title: str, product_type: str | None) -> str:
    if product_type:
        return product_type
    lowered = title.lower()
    for label, keywords in CATEGORY_RULES:
        if any(word in lowered for word in keywords):
            return label
    return "Other"


def _money(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _option_value(variant: dict, name: str) -> str | None:
    for option in variant.get("selectedOptions") or []:
        if option["name"].casefold() == name.casefold():
            return option["value"]
    return None


def _colour_of(variant: dict) -> str | None:
    return _option_value(variant, "Color") or _option_value(variant, "Colour")


def _options_of(product: dict) -> dict[str, list[str]]:
    return {o["name"]: o["values"] for o in product.get("options") or []}


async def _active_products(handles: list[str] | None = None) -> list[dict]:
    """Live ACTIVE products. A draft or archived product is never returned."""
    query = "status:ACTIVE"
    if handles:
        joined = " OR ".join(f"handle:{h}" for h in handles)
        query = f"({joined}) AND status:ACTIVE"
    data = await graphql(CATALOGUE, {"query": query, "first": MAX_PRODUCTS, "variants": MAX_VARIANTS})
    return data["products"]["nodes"]


# The store tags a piece Boys, Girls or Baby. Without that on the catalogue the
# agent can only guess from the title, and it guesses badly: asked for a
# 9-year-old boy it offered the one dress that happened to run to 10Y.
AUDIENCE_TAGS = ("Boys", "Girls", "Baby")


def _suits(tags: list[str] | None) -> list[str]:
    """Who a piece is for, from the store's own tags. Empty means either."""
    lowered = {t.strip().lower() for t in tags or []}
    return [name for name in AUDIENCE_TAGS if name.lower() in lowered]


async def browse_catalogue() -> dict:
    """Everything a shopper can buy, grouped by category so a look can be composed."""
    currency = (await shop_info())["currency"]
    products = []
    for node in await _active_products():
        variants = node["variants"]["nodes"]
        prices = [_money(v["price"]) for v in variants if v.get("price")]
        options = _options_of(node)
        products.append(
            {
                "handle": node["handle"],
                "product_id": node.get("legacyResourceId"),
                "title": node["title"],
                "category": _category(node["title"], node.get("productType")),
                "for": _suits(node.get("tags")),
                "price_from": float(min(prices)) if prices else None,
                "price_to": float(max(prices)) if prices else None,
                "in_stock": any(v["availableForSale"] for v in variants),
                "colors": options.get("Color") or options.get("Colour") or [],
                "sizes": options.get("Size") or [],
                "image": product_image(node),
                "url": product_url(node),
            }
        )
    by_category: dict[str, list[str]] = {}
    for product in products:
        by_category.setdefault(product["category"], []).append(product["handle"])
    return {
        "currency": currency,
        "count": len(products),
        "categories": by_category,
        "products": products,
    }


# ── Adding to the shopper's bag ────────────────────────────────────────────
# The cart lives in the shopper's browser session, not here, so nothing below
# writes to it. What they asked for is resolved to exact, buyable variants, and
# the result carries an instruction the widget carries out with the storefront's
# own cart API. _match_variant alone would happily take the first in-stock size
# when none was named - fine for pricing a look, wrong for someone's bag - so a
# real choice left unmade comes back as a question instead.

VARIANT_BY_ID = """
query CartVariant($id: ID!) {
  productVariant(id: $id) {
    legacyResourceId
    title
    price
    availableForSale
    media(first: 1) { nodes { ... on MediaImage { image { url } } } }
    product {
      legacyResourceId
      title
      handle
      status
      onlineStoreUrl
      featuredMedia { ... on MediaImage { image { url } } }
    }
  }
}
"""

MAX_CART_QUANTITY = 10


def _cart_line(product: dict, variant: dict, quantity: int) -> dict:
    unit = _money(variant["price"])
    variant_id = variant.get("legacyResourceId")
    return {
        "variant_id": variant_id,
        "product_id": product.get("legacyResourceId"),
        "title": product["title"],
        "option": None if variant.get("title") == "Default Title" else variant.get("title"),
        "quantity": quantity,
        "unit_price": float(unit),
        "line_total": float(unit * quantity),
        "image": variant_image(variant) or product_image(product),
        "url": product_url(product, variant_id),
    }


async def cart_additions(items: list[dict]) -> dict:
    """What to add to the bag, as exact variants, and the instruction to add them.

    Each item names a product ("Catherine gingham dress", or a handle) with its
    colour, size and quantity - or gives a variant id a tool already produced,
    such as build_outfit's cart_items. The instruction is only issued when every
    item resolved: half a request quietly added is worse than one more question.
    """
    from app.services import compare
    from app.services.shopify_storefront import collection_tree

    currency = (await shop_info())["currency"]
    lines: list[dict] = []
    needs_choice: list[dict] = []
    problems: list[dict] = []
    tree: dict | None = None

    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            quantity = min(MAX_CART_QUANTITY, max(1, int(item.get("quantity") or 1)))
        except (TypeError, ValueError):
            quantity = 1

        variant_id = str(item.get("variant_id") or "").strip()
        if variant_id:
            node = (
                await graphql(VARIANT_BY_ID, {"id": f"gid://shopify/ProductVariant/{variant_id}"})
            )["productVariant"]
            product = (node or {}).get("product") or {}
            if not node or product.get("status") != "ACTIVE":
                problems.append({"variant_id": variant_id, "reason": "not_found_or_not_for_sale"})
            elif not node["availableForSale"]:
                problems.append({"title": product["title"], "option": node["title"], "reason": "out_of_stock"})
            else:
                lines.append(_cart_line(product, node, quantity))
            continue

        name = str(item.get("product") or item.get("handle") or item.get("title") or "").strip()
        if not name:
            problems.append({"reason": "no_product_named"})
            continue
        if tree is None:
            tree = await collection_tree()
        handle = name if name in tree["handles"].values() else None
        if handle is None and " ".join(name.lower().split()) not in tree["ids"]:
            rivals = compare.matches(name, tree["ids"])
            if len(rivals) > 1:
                # Two products answer to the name ("Catherine gingham dress" is two
                # listings). A comparison can live with the nearer one; a bag cannot.
                needs_choice.append({
                    "asked_for": name,
                    "missing": ["product"],
                    "which_product": [tree["titles"][tree["ids"][t]] for t in rivals[:4]],
                })
                continue
        if handle is None:
            product_id, near = compare.resolve(name, tree["ids"], tree["titles"])
            if product_id is None:
                problems.append({"asked_for": name, "reason": "not_found", "did_you_mean": near})
                continue
            handle = tree["handles"][product_id]
        product = next(iter(await _active_products([handle])), None)
        if product is None:
            problems.append({"asked_for": name, "reason": "not_found_or_not_for_sale"})
            continue

        options = _options_of(product)
        colours = options.get("Color") or options.get("Colour") or []
        sizes = options.get("Size") or []
        colour = item.get("color") or item.get("colour") or (colours[0] if len(colours) == 1 else None)
        size = item.get("size") or (sizes[0] if len(sizes) == 1 else None)
        missing = [name for name, chosen, offered in (("color", colour, colours), ("size", size, sizes))
                   if not chosen and len(offered) > 1]
        if missing:
            needs_choice.append({
                "title": product["title"],
                "missing": missing,
                "available_colors": colours,
                "available_sizes": sizes,
            })
            continue

        variant = _match_variant(product, colour, size)
        if variant is None:
            problems.append({"title": product["title"], "reason": "no_variant_for_that_choice",
                             "available_colors": colours, "available_sizes": sizes})
        elif not variant["availableForSale"]:
            problems.append({"title": product["title"], "option": variant["title"], "reason": "out_of_stock"})
        else:
            lines.append(_cart_line(product, variant, quantity))

    done = bool(lines) and not needs_choice and not problems
    result: dict = {
        "done": done,
        "currency": currency,
        "lines": lines,
        "needs_choice": needs_choice,
        "problems": problems,
    }
    if done:
        result["action"] = {
            "type": "add_to_cart",
            "items": [{"variant_id": line["variant_id"], "quantity": line["quantity"]} for line in lines],
        }
    return result


# ── Suggesting as the conversation goes ────────────────────────────────────
# An outfit conversation used to be an interrogation - age, occasion, colour,
# budget, one reply after another with nothing to look at. Told to show pieces
# along the way, the model skipped the lookup and invented them ("Boys' Kurta
# Pyjama, 1,299 INR"). So the filtering lives here, in code: the agent passes
# what it knows and names what comes back.

# Right for a newborn, the wrong answer to "what should he wear to a party".
_NURSERY_BASICS = {"Bib", "Blanket", "Sleepsuit", "Mittens", "Socks"}

_WHO = {
    "boy": "Boys", "boys": "Boys", "son": "Boys", "him": "Boys",
    "girl": "Girls", "girls": "Girls", "daughter": "Girls", "her": "Girls",
    "baby": "Baby", "newborn": "Baby", "infant": "Baby",
}

SUGGESTION_LIMIT = 4


def _fits_age(sizes: list[str], age: int | None) -> bool:
    """Whether a piece comes in a size for this age. Pieces with no size run fit."""
    if age is None or not sizes:
        return True
    for raw in sizes:
        size = raw.strip().upper()
        if size == "ONE SIZE" or "UK" in size or "EU" in size:
            return True                 # shoe sizes do not map to an age
        if size == f"{age}Y":
            return True
        if age <= 1 and size.endswith("M"):
            return True
        if size.endswith("Y") and "-" in size:
            low, _, high = size[:-1].partition("-")
            if low.isdigit() and high.isdigit() and int(low) <= age <= int(high):
                return True
    return False


def _colour_match(colours: list[str], wanted: str) -> str | None:
    """The piece's own name for the colour asked for - "navy" finds "Navy"."""
    for colour in colours:
        if wanted in colour.lower():
            return colour
    return None


async def suggest_pieces(for_who: str = "", colour: str = "", occasion: str = "",
                         age: int | None = None, budget: float | None = None,
                         limit: int = SUGGESTION_LIMIT) -> dict:
    """A few in-stock pieces that suit what the shopper has said so far.

    Every filter is optional, so the first message of a conversation already
    gets something to look at. One piece per category, so the row reads as the
    start of an outfit rather than four versions of the same shirt.
    """
    catalogue = await browse_catalogue()
    audience = _WHO.get((for_who or "").strip().lower())
    wanted = (colour or "").strip().lower()
    age = int(age) if age else None

    pool = [p for p in catalogue["products"] if p["in_stock"]]
    if audience:
        pool = [p for p in pool if not p["for"] or audience in p["for"]]
    if occasion:
        pool = [p for p in pool if p["category"] not in _NURSERY_BASICS]
    if age is not None:
        pool = [p for p in pool if _fits_age(p["sizes"], age)]
    if budget:
        pool = [p for p in pool if p["price_from"] is not None and p["price_from"] <= budget]

    colour_matched = None
    if wanted:
        coloured = [p for p in pool if _colour_match(p["colors"], wanted)]
        colour_matched = bool(coloured)
        # Asked for navy, shown navy - padding the row with other colours would
        # have the agent calling a powder-blue shirt navy.
        if coloured:
            pool = coloured

    # A piece tagged for this child beats one that merely suits either.
    pool.sort(key=lambda p: 0 if audience and audience in p["for"] else 1)

    picked, seen = [], set()
    for product in pool:
        if product["category"] in seen:
            continue
        seen.add(product["category"])
        picked.append(product)
        if len(picked) >= max(1, limit):
            break

    still_to_ask = [name for name, have in (("age", age), ("budget", budget)) if not have]
    return {
        "currency": catalogue["currency"],
        "known": {"for": audience, "colour": colour or None, "occasion": occasion or None,
                  "age": age, "budget": budget or None},
        "colour_matched": colour_matched,
        "still_to_ask": still_to_ask,
        "count": len(picked),
        "products": [
            {
                "handle": p["handle"],
                "product_id": p["product_id"],
                "title": p["title"],
                "category": p["category"],
                "price_from": p["price_from"],
                "currency": catalogue["currency"],
                "colour": _colour_match(p["colors"], wanted) if wanted else None,
                "colors": p["colors"],
                "sizes": p["sizes"],
                "image": p["image"],
                "url": p["url"],
            }
            for p in picked
        ],
    }


def _affinity(product: dict, categories: set[str], tags: set[str]) -> int:
    """How well a product matches what this shopper has bought before."""
    score = 0
    if product["category"] and product["category"].casefold() in categories:
        score += 2
    score += len(tags & {tag.casefold() for tag in product.get("tags") or []})
    return score


async def recommend_from_orders(orders: list[dict], limit: int = 4) -> dict:
    """Suggest live products that suit what a shopper has bought before.

    Ranks the catalogue by shared category and tags with their past purchases,
    and never suggests something they already own. Falls back to the rest of the
    catalogue when nothing matches, so the shopper always gets an answer.
    """
    bought_handles: set[str] = set()
    categories: set[str] = set()
    tags: set[str] = set()
    bought: list[dict] = []
    category_counts: dict[str, int] = {}
    for order in orders:
        for item in order.get("items") or []:
            if item.get("handle"):
                bought_handles.add(item["handle"])
            if item.get("category"):
                categories.add(item["category"].casefold())
                category_counts[item["category"]] = category_counts.get(item["category"], 0) + 1
            item_tags = {tag.casefold() for tag in item.get("tags") or []} - _HOUSEKEEPING_TAGS
            tags.update(item_tags)
            if item.get("title"):
                bought.append({"title": item["title"], "category": item.get("category") or "",
                               "tags": item_tags})
    # Housekeeping tags every product carries say nothing about taste.
    tags -= _HOUSEKEEPING_TAGS

    currency = (await shop_info())["currency"]
    candidates = []
    for node in await _active_products():
        if node["handle"] in bought_handles:
            continue
        variants = node["variants"]["nodes"]
        if not any(v["availableForSale"] for v in variants):
            continue
        prices = [_money(v["price"]) for v in variants if v.get("price")]
        candidates.append(
            {
                "handle": node["handle"],
                "product_id": node.get("legacyResourceId"),
                "title": node["title"],
                "category": _category(node["title"], node.get("productType")),
                "tags": node.get("tags") or [],
                "price_from": float(min(prices)) if prices else None,
                "about": _first_sentence(node.get("description")),
                "image": product_image(node),
                "url": product_url(node),
            }
        )

    ranked = sorted(candidates, key=lambda p: _affinity(p, categories, tags), reverse=True)
    picks = ranked[:limit]
    for pick in picks:
        pick["because"], pick["like_purchase"] = _reason(pick, bought)
        pick.pop("tags", None)

    # What they keep coming back for, most often first - the opening line of
    # the reply ("since you've gone for dresses and cardigans...").
    interests = sorted(category_counts, key=lambda c: -category_counts[c])
    return {
        "currency": currency,
        "based_on_orders": len(orders),
        "interests": interests,
        "heading": _heading(picks),
        "already_owned": sorted(bought_handles),
        "count": len(picks),
        "products": picks,
    }


_HOUSEKEEPING_TAGS = {"all products", "in-stock", "top products", "best seller"}


def _reason(pick: dict, bought: list[dict]) -> tuple[str, str | None]:
    """Why this pick, tied to the actual thing they bought - not "matches your
    history", which gave the agent nothing to say and every card the same line."""
    category = (pick.get("category") or "").casefold()
    for item in bought:
        if category and item["category"].casefold() == category:
            return f"another {pick['category'].lower()}, like the {item['title']} they bought", item["title"]
    pick_tags = {t.casefold() for t in pick.get("tags") or []} - _HOUSEKEEPING_TAGS
    for item in bought:
        if item["tags"] & pick_tags:
            return f"in the same style as the {item['title']} they bought", item["title"]
    return "popular with other shoppers", None


def _first_sentence(text: str | None, limit: int = 160) -> str | None:
    """The product's own opening line, so a reason is quoted rather than invented."""
    if not text:
        return None
    text = " ".join(text.split())
    end = text.find(". ")
    first = text[: end + 1] if 0 < end < limit else text[:limit]
    return first.strip() or None


def _heading(picks: list[dict]) -> str | None:
    """A title for the row of cards: "Picked for you: Dresses, Cardigans and Hairbands"."""
    from app.services.suggestions import plural

    names = list(dict.fromkeys(plural(p["category"]) for p in picks if p.get("category")))
    if not names:
        return None
    joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return f"Picked for you: {joined}"


def _parse_items(raw: str | list | dict) -> list[dict]:
    """Read the items the agent chose.

    Models are inconsistent here: some send a JSON string, some send the array
    itself, and some wrap it in a code fence. All three are accepted rather than
    failing a shopper's turn over a formatting detail.
    """
    items = raw
    if isinstance(items, str):
        text = items.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        items = json.loads(text)
    if isinstance(items, dict):
        items = items.get("items") or [items]
    if not isinstance(items, list):
        raise ValueError("items must be a JSON array")
    return [i for i in items[:MAX_OUTFIT_ITEMS] if isinstance(i, dict)]


def _match_variant(product: dict, want_colour: str | None, want_size: str | None) -> dict | None:
    """Pick the variant matching the requested colour and size, preferring one in stock."""

    def matches(variant: dict) -> bool:
        if want_colour:
            value = _colour_of(variant)
            if not value or value.casefold() != want_colour.casefold():
                return False
        if want_size:
            value = _option_value(variant, "Size")
            if not value or value.casefold() != want_size.casefold():
                return False
        return True

    candidates = [v for v in product["variants"]["nodes"] if matches(v)]
    if not candidates:
        return None
    return next((v for v in candidates if v["availableForSale"]), candidates[0])


async def build_outfit(items: str | list, budget: float | None = None) -> dict:
    """Price a chosen look exactly and name the variants the frontend should add."""
    try:
        requested = _parse_items(items)
    except (ValueError, json.JSONDecodeError) as exc:
        return {"error": f"Could not read the outfit items: {exc}", "expected_format": OUTFIT_FORMAT}

    handles = [str(i.get("handle", "")).strip() for i in requested if i.get("handle")]
    if not handles:
        return {"error": "No product handles given.", "expected_format": OUTFIT_FORMAT}

    currency = (await shop_info())["currency"]
    products = {p["handle"]: p for p in await _active_products(handles)}

    chosen: list[dict] = []
    problems: list[dict] = []
    total = Decimal("0")

    for item in requested:
        handle = str(item.get("handle", "")).strip()
        colour = item.get("color") or item.get("colour") or None
        size = item.get("size") or None
        try:
            quantity = max(1, int(item.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1

        product = products.get(handle)
        if product is None:
            problems.append({"handle": handle, "reason": "not_found_or_not_for_sale"})
            continue

        variant = _match_variant(product, colour, size)
        if variant is None:
            options = _options_of(product)
            problems.append(
                {
                    "handle": handle,
                    "title": product["title"],
                    "reason": "no_variant_for_that_choice",
                    "available_colors": options.get("Color") or options.get("Colour") or [],
                    "available_sizes": options.get("Size") or [],
                }
            )
            continue

        if not variant["availableForSale"]:
            problems.append(
                {
                    "handle": handle,
                    "title": product["title"],
                    "option": variant["title"],
                    "reason": "out_of_stock",
                }
            )
            continue

        unit_price = _money(variant["price"])
        line_total = unit_price * quantity
        total += line_total
        variant_id = variant.get("legacyResourceId")
        chosen.append(
            {
                "handle": handle,
                "product_id": product.get("legacyResourceId"),
                "variant_id": variant_id,
                "title": product["title"],
                "category": _category(product["title"], product.get("productType")),
                "option": None if variant["title"] == "Default Title" else variant["title"],
                "sku": variant.get("sku") or None,
                "unit_price": float(unit_price),
                "quantity": quantity,
                "line_total": float(line_total),
                # The variant's own photo when it has one, so a pink shoe shows pink.
                "image": variant_image(variant) or product_image(product),
                "url": product_url(product, variant_id),
            }
        )

    result: dict = {
        "outfit": chosen,
        "item_count": len(chosen),
        "currency": currency,
        "total": float(total),
        "problems": problems,
    }

    if budget:
        allowance = _money(budget)
        result["budget"] = float(allowance)
        result["within_budget"] = total <= allowance
        difference = allowance - total
        result["remaining" if difference >= 0 else "over_by"] = float(abs(difference))

    # The frontend adds the look to the bag itself, so it just needs the variants.
    result["cart_items"] = [
        {"variant_id": c["variant_id"], "quantity": c["quantity"]} for c in chosen
    ]
    return result


__all__ = ["OUTFIT_FORMAT", "ShopifyError", "browse_catalogue", "build_outfit"]
