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
import re
from decimal import Decimal, InvalidOperation

from app.services import occasions
from app.services.shopify_client import ShopifyError, graphql
from app.services.shopify_storefront import (
    product_image,
    product_url,
    shop_info,
    variant_image,
)

logger = logging.getLogger(__name__)

MAX_PRODUCTS = 50
# How much of the catalogue a look may be built from. Paged in, so this is a
# guard against an enormous shop rather than the size of a normal one.
CATALOGUE_CEILING = 500
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
query OutfitCatalogue($query: String!, $first: Int!, $variants: Int!, $cursor: String) {
  products(first: $first, after: $cursor, query: $query, sortKey: TITLE) {
    pageInfo { hasNextPage endCursor }
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


def _named_category(name: str, products: list[dict]) -> str | None:
    """The kind of piece the shopper named, as this store labels it.

    They say "dress" or "coats"; the store's own product types say "Dress" and
    "Coat". Match against what the catalogue actually holds rather than a list
    of our own, so a store that calls them "Outerwear" works too.
    """
    from app.services.store_profile import _singular

    want = _singular(" ".join((name or "").strip().lower().split()))
    if not want:
        return None
    have = {p["category"] for p in products if p.get("category")}
    for label in sorted(have, key=len):
        lowered = _singular(label)
        if lowered == want or want in lowered or lowered in want:
            return label
    guess = _category(want, None)
    return guess if guess in have else None


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
    from app.services.shopify_storefront import sellable

    joined = " OR ".join(f"handle:{h}" for h in handles) if handles else ""
    query = sellable(f"({joined})" if joined else "")
    # One page used to be the whole catalogue, so a shop with more products than
    # that had the rest of the alphabet missing: a look could not be built around
    # a coat whose name began with S, and the agent said we did not sell it.
    found: list[dict] = []
    cursor = None
    while len(found) < CATALOGUE_CEILING:
        data = await graphql(CATALOGUE, {"query": query, "first": MAX_PRODUCTS,
                                         "variants": MAX_VARIANTS, "cursor": cursor})
        page = data["products"]
        found.extend(page["nodes"])
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
    return found


# The store tags a piece Boys, Girls or Baby. Without that on the catalogue the
# agent can only guess from the title, and it guesses badly: asked for a
# 9-year-old boy it offered the one dress that happened to run to 10Y.
AUDIENCE_TAGS = ("Boys", "Girls", "Baby")


# Pieces the store's own words place on one child or the other, where it has
# not tagged them. A big bow hairband arrived in a ten year old boy's outfit
# because nothing said whose it was.
HERS = ("hairband", "headband", "bow", "frill", "ruffle", "tutu", "ballet", "pinafore")
HIS = ("tie", "braces", "bow tie", "waistcoat")


def _named_for(title: str, audience: str) -> bool:
    """Whether a piece's own name puts it on the other child."""
    lowered = (title or "").lower()
    other = HERS if audience == "Boys" else HIS if audience == "Girls" else ()
    return any(word in lowered for word in other)


def _for_this_child(pool: list[dict], audience: str | None) -> list[dict]:
    """Only pieces that suit this child - by tag, and by name.

    An untagged "Boy's Belt" is still a boy's belt, and it has no place in a
    look built around a girl's dress.
    """
    if not audience:
        return pool
    kept = [p for p in pool if not p["for"] or audience in p["for"]]
    other = {"Girls": "boy", "Boys": "girl"}.get(audience)
    if other:
        kept = [p for p in kept if other not in (p.get("title") or "").lower()]
    # Untagged, but the name says whose it is - only where the store itself has
    # not tagged the piece for this child.
    return [p for p in kept if audience in (p.get("for") or [])
            or not _named_for(p.get("title") or "", audience)]


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
                # What it IS, for the store's own shelves, versus what it DOES
                # in an outfit. A "Coat" and a "Jacket" are two product types
                # and one role, and only the role knows what goes with what.
                "role": _category(node["title"], None),
                "for": _suits(node.get("tags")),
                "occasions": occasions.of(node.get("title"), " ".join(node.get("tags") or []),
                                          node.get("description")),
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


async def _variant(variant_id: str) -> dict | None:
    """One variant, read twice if the first read fails.

    A bag is built from several of these in a row, and losing the whole
    request to one blip - the shopper is told the store cannot be reached
    while their pieces sit there - is worth a second attempt.
    """
    gid = f"gid://shopify/ProductVariant/{variant_id}"
    try:
        return (await graphql(VARIANT_BY_ID, {"id": gid}))["productVariant"]
    except ShopifyError as exc:
        logger.info("Re-reading variant %s after %s", variant_id, exc)
        return (await graphql(VARIANT_BY_ID, {"id": gid}))["productVariant"]


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
            node = await _variant(variant_id)
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


def _their_colour_of(product: dict | None) -> str | None:
    """The shopper's colour, as this product spells it - None if it has no such variant.

    Asked for blue and handed a look, they should get the blue one of anything
    that comes in blue, without having to say it again for every piece.
    """
    from app.services import shopper_identity as identity

    wanted = identity.wants_colour()
    if not product or not wanted:
        return None
    options = _options_of(product)
    values = options.get("Color") or options.get("Colour") or []
    words = {w for w in re.findall(r"[a-z]+", wanted) if len(w) > 2}
    for value in values:
        if words & {w for w in re.findall(r"[a-z]+", str(value).lower()) if len(w) > 2}:
            return value
    return None


def _colour_match(colours: list[str], wanted: str) -> str | None:
    """The piece's own name for the colour asked for - "navy" finds "Navy"."""
    for colour in colours:
        if wanted in colour.lower():
            return colour
    return None


async def suggest_pieces(for_who: str = "", colour: str = "", occasion: str = "",
                         age: int | None = None, budget: float | None = None,
                         category: str = "", limit: int = SUGGESTION_LIMIT) -> dict:
    """A few in-stock pieces that suit what the shopper has said so far.

    Every filter is optional, so the first message of a conversation already
    gets something to look at.

    Without a category this returns one piece per kind, so the row reads as the
    start of an outfit rather than four versions of the same shirt. With one -
    "a dress for a wedding" - it returns that kind of piece and nothing else,
    because a shopper asking for dresses wants to choose between dresses.

    An occasion ranks rather than filters: the pieces whose own name, tags or
    description place them at that occasion come first, a neighbouring occasion
    next, and the rest after - so there is always something to show.
    """
    from app.services import shopper_identity as identity

    catalogue = await browse_catalogue()
    audience = _WHO.get((for_who or "").strip().lower()) or identity.shopping_for()
    # The agent does not always pass on what the shopper said; the preference
    # is held for the turn either way, so a suggestion never loses it.
    wanted = (colour or identity.wants_colour() or "").strip().lower()
    age = int(age) if age else None

    pool = [p for p in catalogue["products"] if p["in_stock"]]
    pool = _for_this_child(pool, audience)
    if occasion and not occasions.is_sleepwear(occasion):
        pool = [p for p in pool if p["category"] not in _NURSERY_BASICS]
        # Asked for a wedding, shown a nightdress: it is a dress by product type.
        pool = [p for p in pool if not occasions.is_sleepwear(p["title"])]
    if age is not None:
        pool = [p for p in pool if _fits_age(p["sizes"], age)]
    if budget:
        pool = [p for p in pool if p["price_from"] is not None and p["price_from"] <= budget]

    # "A dress for a wedding": show dresses, not one dress and three other things.
    in_stock = [p for p in catalogue["products"] if p["in_stock"]]
    wanted_category = _named_category(category, pool) or _named_category(category, in_stock) \
        if category else None
    category_note = None
    if wanted_category:
        of_kind = [p for p in pool if p["category"] == wanted_category]
        if of_kind:
            pool = of_kind
        else:
            # The shop does sell them, just not to this child. Saying "we have no
            # coats" would be a lie; saying nothing at all reads as one too.
            elsewhere = [p for p in in_stock if p["category"] == wanted_category]
            if elsewhere:
                whose = sorted({who for p in elsewhere for who in p["for"]})
                category_note = {
                    "kind": wanted_category,
                    "none_for_this_child": True,
                    "we_do_have": len(elsewhere),
                    "but_only_for": ", ".join(whose) or None,
                    "in_sizes": sorted({s for p in elsewhere for s in (p["sizes"] or [])})[:12],
                }
            wanted_category = None

    colour_matched = None
    if wanted:
        coloured = [p for p in pool if _colour_match(p["colors"], wanted)]
        colour_matched = bool(coloured)
        # Asked for navy, shown navy - padding the row with other colours would
        # have the agent calling a powder-blue shirt navy.
        if coloured:
            pool = coloured

    # Best fit first: the occasion the store's words actually place it at, then
    # a piece tagged for this child over one that merely suits either.
    pool.sort(key=lambda p: (
        -occasions.score(p.get("occasions"), occasion),
        0 if audience and audience in p["for"] else 1,
        p["price_from"] or 0,
    ))

    picked, seen = [], set()
    for product in pool:
        # One per kind only when the shopper did not name a kind.
        if not wanted_category:
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
                  "age": age, "budget": budget or None, "category": wanted_category},
        "category_note": category_note,
        "occasion_matched": bool(occasion) and any(
            occasions.score(p.get("occasions"), occasion) >= 2 for p in picked),
        "colour_matched": colour_matched,
        "still_to_ask": still_to_ask,
        "count": len(picked),
        "products": [
            {
                "handle": p["handle"],
                "product_id": p["product_id"],
                "title": p["title"],
                "category": p["category"],
                "worn_for": ", ".join(p.get("occasions") or []) or None,
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


# What goes with what, by the store's own categories. First match wins, and the
# piece being looked at is never paired with another of its own kind.
# What goes with what, by the part each piece plays rather than by the name the
# store files it under. These are roles from the product's own name (see
# CATEGORY_RULES): a shirt and a jumper are both "Top", a coat and a jacket are
# both "Outerwear". Nothing here names a product, so it holds for any stock.
COMPANIONS = {
    "Dress": ("Outerwear", "Shoes", "Accessory", "Top"),
    "Top": ("Bottoms", "Shoes", "Outerwear", "Accessory"),
    "Bottoms": ("Top", "Shoes", "Outerwear", "Accessory"),
    "Outerwear": ("Top", "Bottoms", "Dress", "Shoes"),
    "Shoes": ("Dress", "Top", "Bottoms", "Accessory"),
    "Accessory": ("Dress", "Top", "Bottoms", "Shoes"),
    "Other": ("Top", "Bottoms", "Dress", "Shoes"),
}
DEFAULT_COMPANIONS = ("Top", "Bottoms", "Shoes", "Accessory")
# Colours that sit with anything, so a look is never blocked on an exact match.
NEUTRALS = {"white", "ivory", "cream", "navy", "grey", "gray", "beige", "black", "camel", "stone"}
LOOK_PIECES = 3


def _colour_words(piece: dict) -> set[str]:
    """Every colour word this piece answers to, its name included.

    The store writes "Blue Denim" and "Baby Pink"; a shopper says "blue" and
    "pink". Matching whole strings missed both.
    """
    words: set[str] = set()
    for value in list(piece.get("colors") or []) + [piece.get("title") or ""]:
        words |= {w for w in re.findall(r"[a-z]+", str(value).lower()) if len(w) > 2}
    return words


def _comes_in(piece: dict, colour: str | None) -> bool:
    """Whether the shopper could actually have this piece in the colour they asked for."""
    if not colour:
        return False
    wanted = {w for w in re.findall(r"[a-z]+", colour.lower()) if len(w) > 2}
    return bool(wanted & _colour_words(piece))


def _shares_colour(piece: dict, colours: list) -> bool:
    theirs = _colour_words(piece)
    anchor = {w for c in colours for w in re.findall(r"[a-z]+", str(c).lower()) if len(w) > 2}
    return bool(theirs & anchor) or bool(theirs & NEUTRALS)


def _age_of(size: str | None) -> int | None:
    """The age a size label implies: "8Y" is 8, "12M" is 1, "2-3Y" is 3."""
    if not size:
        return None
    label = size.strip().upper()
    years = re.findall(r"(\d{1,2})\s*Y", label)
    if years:
        return int(years[-1])
    months = re.findall(r"(\d{1,2})\s*M", label)
    if months:
        return max(0, round(int(months[-1]) / 12))
    return None


def _suits_age(piece: dict, age: int | None) -> bool:
    """Whether a piece is sold in a size for a child this old.

    Only sizes that say an age count: "S/M/L", "One Size" and shoe sizes say
    nothing about it, so they are left alone. A dummy sold in 0-6M and 18M+ is
    not part of a ten year old's outfit, and that is what this keeps out.
    """
    if age is None:
        return True
    if age >= OUT_OF_THE_PRAM and any(w in (piece.get("title") or "").lower() for w in BABY_ONLY):
        return False
    told = [s for s in (piece.get("sizes") or []) if re.search(r"\d\s*[MY]\b", s.strip().upper())]
    return _fits_age(told, age) if told else True


# Pieces made for a baby, whatever their sizes say. Booties and pram shoes come
# in "OS" or in a run of small numbers that names no age, so nothing else keeps
# them out of a ten year old's outfit. These are words a store writes about its
# own products, not a list of products.
BABY_ONLY = ("bootie", "bootee", "pram", "newborn", "swaddle", "dummy", "pacifier",
             "bib ", "bibs", "teether", "rattle", "sleepsuit")
OUT_OF_THE_PRAM = 3     # from this age on, a baby piece is the wrong piece


def _size_number(label: str) -> float | None:
    """The number a shoe size means, from a label that may carry three of them.

    "13.5UK/1.5US/32EU" is one shoe written three ways. Taking the first number
    put a two year old in a 32 - so the European figure wins where the label
    says which is which, and otherwise the largest, since UK and US run smaller.
    """
    european = re.search(r"(\d+(?:\.\d+)?)\s*EU", label, re.I)
    if european:
        return float(european.group(1))
    found = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", label)]
    return max(found) if found else None


def _numbers_in(sizes: list[str]) -> list[float]:
    return [n for x in sizes if (n := _size_number(x)) is not None]


def _shoe_span(products: list[dict]) -> tuple[float, float] | None:
    """The run of numbered sizes the shop sells, smallest to largest."""
    numbers = [n for p in products
               for n in _numbers_in([x for x in (p.get("sizes") or [])
                                     if not re.search(r"\d\s*[MY]\b", x.strip().upper())])]
    return (min(numbers), max(numbers)) if numbers else None


def _numbered_size_fits(piece: dict, age: int | None, oldest: int | None,
                        span: tuple[float, float] | None) -> bool:
    """Whether a piece sized by number - shoes - is made for a child this old.

    A baby bootie in 20-26EU and a plimsoll in 21-36EU look alike to anything
    that only reads sizes as text. Placing the child on the shop's own run of
    numbers tells them apart: an eight year old lands around 31, which the
    plimsoll covers and the bootie does not.
    """
    if age is None or not span or not oldest:
        return True
    sizes = [x for x in (piece.get("sizes") or []) if not re.search(r"\d\s*[MY]\b", x.strip().upper())]
    numbers = _numbers_in(sizes)
    if not numbers or len(sizes) != len(piece.get("sizes") or []):
        return True                     # sized by age, or not sized at all
    low, high = span
    target = low + (min(age, oldest) / oldest) * (high - low)
    return min(numbers) - 1 <= target <= max(numbers) + 1


def _size_for_age(sizes: list[str], age: int | None, oldest: int | None,
                  span: tuple[float, float] | None = None) -> str:
    """The size to put in the bag for a child this old.

    Sizes that name an age answer for themselves. Shoe sizes do not - they are
    numbers - so they are read as a run: a child two thirds of the way up the
    ages the shop sells takes a shoe two thirds of the way up its numbers. The
    run and the span both come from the store, so nothing here assumes a chart.
    """
    if not sizes:
        return ""
    told = [x for x in sizes if re.search(r"\d\s*[MY]\b", x.strip().upper())]
    if told:
        fits = [x for x in told if _fits_age([x], age)] if age is not None else []
        return (fits or told)[0]
    numbered = sorted((n, x) for x in sizes if (n := _size_number(x)) is not None)
    if not numbered or age is None or not oldest:
        return sizes[0]
    if span:
        low, high = span
        target = low + (min(age, oldest) / oldest) * (high - low)
        return min(numbered, key=lambda pair: abs(pair[0] - target))[1]
    at = round((min(age, oldest) / oldest) * (len(numbered) - 1))
    return numbered[max(0, min(at, len(numbered) - 1))][1]


def _same_size(piece: dict, size: str | None) -> bool:
    """Whether a piece comes in the size the look is being built in."""
    if not size:
        return True
    from app.services.size_finder import span_of

    want = span_of(size)
    if want is None:
        return True
    labels = piece.get("sizes") or []
    if not labels:
        return True                      # one-size pieces go with everything
    for label in labels:
        span = span_of(label)
        if span is None:                 # shoe sizes: not comparable to age sizes
            return True
        if span[0] <= want[1] and want[0] <= span[1]:
            return True
    return False


async def complete_the_look(product: str, size: str | None = None,
                            budget: float | None = None, pieces: int = LOOK_PIECES) -> dict:
    """The coordinated outfit around the piece a shopper is looking at.

    One companion per category - a cardigan, shoes, an accessory - in stock, for
    the same child, in their size and sitting with the colours; then the whole
    look is priced through build_outfit so every line is a real variant the
    storefront can add to the bag.
    """
    catalogue = await browse_catalogue()
    wanted = " ".join((product or "").lower().split())
    stock = [p for p in catalogue["products"] if p["in_stock"]]
    anchor = next((p for p in stock if p["handle"] == wanted), None)
    if anchor is None and wanted:
        anchor = next((p for p in stock if wanted in p["title"].lower()), None)
    if anchor is None and wanted:
        from app.services import compare

        by_title = {p["title"].lower(): p for p in stock}
        close = compare.matches(wanted, {t: t for t in by_title})
        anchor = by_title[close[0]] if close else None
    if anchor is None:
        return {"found": False, "asked_for": product, "reason": "no_such_product"}

    from app.services import shopper_identity as identity

    # A look built for someone who asked for blue should be blue where the shop
    # allows it - the anchor's own colours decide only what goes with what.
    wanted_colour = identity.wants_colour()
    # Whose look this is. An untagged anchor told us nothing, so a bow hairband
    # walked into a ten year old boy's outfit; the conversation knew he was a boy.
    audience = next(iter(anchor["for"]), None) or identity.shopping_for()
    # Whose size to build in: what was asked for, then what this conversation
    # has settled on, and only then the piece's own first size - which is its
    # smallest, and dressed a six year old as a baby.
    size = size or identity.wants_size() or next((s for s in anchor["sizes"] if s), None)
    ages = [a for p in stock for x in (p["sizes"] or []) if (a := _age_of(x))]
    oldest = max(ages) if ages else None
    anchor_role = anchor.get("role") or _category(anchor["title"], None)
    order = COMPANIONS.get(anchor_role, DEFAULT_COMPANIONS)
    # A shoe size is a number and says nothing about age, so ask the
    # conversation before giving up on knowing how old the child is.
    age = _age_of(size) or _age_of(identity.wants_size())
    if age is None and size and (number := _size_number(size)) is not None:
        # The anchor is a shoe: read the child back off the run of numbers, so
        # a 21 does not come with clothes for a twelve year old.
        run = _shoe_span(stock)
        if run and run[1] > run[0] and oldest:
            age = max(0, round(oldest * (number - run[0]) / (run[1] - run[0])))

    pool = [p for p in stock if p["handle"] != anchor["handle"]
            and (p.get("role") or _category(p["title"], None)) != anchor_role]
    pool = _for_this_child(pool, audience)
    span = _shoe_span(stock)
    pool = [p for p in pool if _same_size(p, size) and _suits_age(p, age)
            and _numbered_size_fits(p, age, oldest, span)]
    if budget:
        pool = [p for p in pool if (p["price_from"] or 0) <= budget]

    picked = []
    for role in order:
        matches = [p for p in pool if (p.get("role") or _category(p["title"], None)) == role]
        if not matches:
            continue
        matches.sort(key=lambda p: (0 if _comes_in(p, wanted_colour) else 1,
                                    0 if _shares_colour(p, anchor["colors"]) else 1,
                                    p["price_from"] or 0))
        picked.append(matches[0])
        if len(picked) >= max(1, pieces):
            break

    # The named companions for this kind of piece may not all be in stock in
    # their size; rather than a look of one, fill up from whatever else suits.
    if len(picked) < max(1, pieces):
        taken = {p.get("role") or _category(p["title"], None) for p in picked}
        rest = [p for p in pool
                if (p.get("role") or _category(p["title"], None)) in order
                and (p.get("role") or _category(p["title"], None)) not in taken]
        rest.sort(key=lambda p: (0 if _comes_in(p, wanted_colour) else 1,
                                 0 if _shares_colour(p, anchor["colors"]) else 1,
                                 p["price_from"] or 0))
        for piece in rest:
            role = piece.get("role") or _category(piece["title"], None)
            if role in taken:
                continue
            picked.append(piece)
            taken.add(role)
            if len(picked) >= max(1, pieces):
                break

    # A budget is what the shopper will spend on the look, not on each piece:
    # four things under 400 each came to 730. Drop the dearest companions until
    # the whole thing fits, keeping the piece they asked about.
    if budget:
        allowed = float(budget)
        while picked and (anchor["price_from"] or 0) + sum(p["price_from"] or 0 for p in picked) > allowed:
            picked.pop(max(range(len(picked)), key=lambda i: picked[i]["price_from"] or 0))

    def line(piece: dict) -> dict:
        item = {"handle": piece["handle"], "quantity": 1}
        if piece["colors"]:
            shared = {c.strip().lower() for c in anchor["colors"]} | NEUTRALS
            item["color"] = next((c for c in piece["colors"] if c.strip().lower() in shared), piece["colors"][0])
        if piece["sizes"]:
            numbered = not any(re.search(r"\d\s*[MY]\b", x.strip().upper()) for x in piece["sizes"])
            # Sizes that name an age are narrowed to the ones that fit; numbers
            # are not - they are a run, and the child's place on it decides.
            fits = piece["sizes"] if numbered else [
                s for s in piece["sizes"] if _same_size({"sizes": [s]}, size)]
            item["size"] = _size_for_age(fits or piece["sizes"], age, oldest, span)
        return item

    look = await build_outfit([line(anchor), *[line(p) for p in picked]], budget)
    look["found"] = bool(look.get("outfit"))
    look["anchor"] = {"handle": anchor["handle"], "title": anchor["title"], "category": anchor["category"]}
    look["size"] = size
    look["heading"] = f"The coordinated look around the {anchor['title']}"
    return look


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
                # What it IS, for the store's own shelves, versus what it DOES
                # in an outfit. A "Coat" and a "Jacket" are two product types
                # and one role, and only the role knows what goes with what.
                "role": _category(node["title"], None),
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
        colour = item.get("color") or item.get("colour") or _their_colour_of(products.get(
            str(item.get("handle", "")).strip()))
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

    # Asked for blue and handed a burgundy pair of trousers, a shopper deserves
    # to be told which pieces the shop simply does not make in their colour -
    # not to be left spotting it in the pictures.
    from app.services import shopper_identity as identity

    if asked := identity.wants_colour():
        result["asked_for_colour"] = asked
        result["not_in_that_colour"] = [
            line["title"] for line in chosen
            if not _comes_in({"colors": [line.get("option") or ""], "title": line["title"]}, asked)
        ]

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
