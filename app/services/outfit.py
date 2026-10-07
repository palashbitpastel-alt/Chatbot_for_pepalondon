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

import asyncio
import copy
import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation

from app.services import audience as audience_reader
from app.services import occasions, parts, suits
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
      description(truncateAt: 1500)
      category { fullName }
      season: metafield(namespace: "custom", key: "season") { value }
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


# Who a piece is for (Boys, Girls, Baby) is read by the model from the store's
# own tags, and from the name of a piece that carries no such tag - see
# app/services/audience.py. No tag names or garment word lists live here.


def _suits_season(piece: dict, season: str | None) -> bool:
    """Whether a piece belongs in a look for this season: what the merchant
    stated, else what the model read off the piece. Unread means no objection -
    title words ("velvet", "knit") once threw out a summer-wedding dress."""
    if not season:
        return True
    read = suits.suits_season(piece, season)
    return True if read is None else read


def _season_first(piece: dict, season: str | None) -> int:
    """0 for a piece the season calls for, 1 for the rest - a sort key."""
    if not season:
        return 1
    if seasons := suits.seasons_of(piece):
        if any(season.lower() in s.lower() for s in seasons):
            return 0
        return 1 if any("all year" in s.lower() for s in seasons) else 2
    return 1


def _for_this_child(pool: list[dict], audience: str | None) -> list[dict]:
    """Only pieces that suit this child - by tag, and by name.

    An untagged "Boy's Belt" is still a boy's belt, and it has no place in a
    look built around a girl's dress.
    """
    if not audience:
        return pool
    # "for" is the model's reading of the store's tags or of the piece's name;
    # empty means it suits any child.
    return [p for p in pool if not p["for"] or audience in p["for"]]


def _suits(tags: list[str] | None, product_id=None) -> list[str]:
    """Who a piece is for, as the model read the store. Empty means either."""
    return audience_reader.of(tags, product_id)


# The whole catalogue is read by several steps of one reply (the tools, the
# no-lookup check, the kind-of-piece check) and was rescanned page by page each
# time. Held briefly, handed out as copies because callers annotate it. Stock is
# re-read live before anything goes in the bag, so a short hold cannot sell
# something that has just sold out.
_CATALOGUE: tuple[float, dict] | None = None
CATALOGUE_SECONDS = 120


async def browse_catalogue() -> dict:
    """Everything a shopper can buy, grouped by category so a look can be composed."""
    global _CATALOGUE
    if _CATALOGUE and time.monotonic() - _CATALOGUE[0] < CATALOGUE_SECONDS:
        return copy.deepcopy(_CATALOGUE[1])
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
                "taxonomy": (node.get("category") or {}).get("fullName"),
                "season": ((node.get("season") or {}).get("value") or None),
                # What the shop wrote about it, for the model to read (see suits).
                "description": node.get("description") or None,
                "product_type": node.get("productType") or None,
                "tags_text": ", ".join(node.get("tags") or []) or None,
                # What it IS, for the store's own shelves, versus what it DOES
                # in an outfit. A "Coat" and a "Jacket" are two product types
                # and one role, and only the role knows what goes with what.
                "role": None,              # the model's reading, set below
                "for": [],
                "_tags": node.get("tags") or [],
                "occasions": occasions.of(node.get("title"), " ".join(node.get("tags") or []),
                                          node.get("description")),
                "price_from": float(min(prices)) if prices else None,
                "price_to": float(max(prices)) if prices else None,
                "in_stock": any(v["availableForSale"] for v in variants),
                "colors": options.get("Color") or options.get("Colour") or [],
                # Every option value whatever the store calls the option, for
                # readers that must not assume an option is named "Color".
                "option_values": sorted({v for vals in options.values() for v in vals or []}),
                "sizes": options.get("Size") or [],
                # Which colour comes in which size, and whether it is there to
                # buy. Choosing the two apart asked for combinations the shop
                # does not sell, and the piece then fell out of the look.
                "combinations": [
                    {"color": _colour_of(v), "size": _option_value(v, "Size"),
                     "available": bool(v.get("availableForSale"))}
                    for v in variants
                ],
                "image": product_image(node),
                "url": product_url(node),
            }
        )
    # What part each piece plays, from the taxonomy or the model's reading of
    # this shop's product types - not from words in the title.
    await parts.learn([p["category"] for p in products])
    for product in products:
        product["role"] = _role_of(product)
    # Who each piece is for, read by the model from this store's tags, or from
    # the piece's name where it carries none.
    await audience_reader.ensure()
    await audience_reader.learn([{"product_id": p["product_id"], "title": p["title"],
                                  "category": p["category"], "tags": p["_tags"]} for p in products])
    for product in products:
        product["for"] = audience_reader.of(product.pop("_tags"), product["product_id"])
    by_category: dict[str, list[str]] = {}
    for product in products:
        by_category.setdefault(product["category"], []).append(product["handle"])
    result = {
        "currency": currency,
        "count": len(products),
        "categories": by_category,
        "products": products,
    }
    _CATALOGUE = (time.monotonic(), result)
    return copy.deepcopy(result)


async def pieces_per_size(audience: str | None = None) -> dict[str, int]:
    """How many in-stock pieces for this child come in each size the shop sells,
    smallest size first - "8Y: 24, 9Y: 2, 10Y: 27". Facts only: which sizes suit
    a child between sizes is the agent's call."""
    from app.services.size_finder import span_of
    pool = _for_this_child([p for p in (await browse_catalogue())["products"] if p["in_stock"]], audience)
    counts: dict[str, int] = {}
    for p in pool:
        for s in set(p["sizes"] or []):
            counts[s] = counts.get(s, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (span_of(kv[0]) or (999, 999), kv[0])))


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
                # Enough for a checklist row the shopper can finish choosing on.
                "url": product_url(product) if product.get("handle") else None,
                "image": product_image(product),
                "product_id": product.get("legacyResourceId"),
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

_WHO = {
    "boy": "Boys", "boys": "Boys", "son": "Boys", "him": "Boys",
    "girl": "Girls", "girls": "Girls", "daughter": "Girls", "her": "Girls",
    "baby": "Baby", "newborn": "Baby", "infant": "Baby",
}

SUGGESTION_LIMIT = 4

# What a shopper means by "an outfit": something on top, something on the legs,
# shoes, and a piece to finish it - or a dress, which does the first two at once.
# These are parts, not garments: the one thing that does not vary between shops.


def _occasion_score(product: dict, occasion: str | None) -> int:
    """How well this piece suits the occasion, as the model read the piece."""
    return suits.score(product, occasion) if suits.occasions_of(product) else 0


def _is_sleepwear(product: dict) -> bool:
    """Nightwear as the model read the piece; never from words in its name."""
    return suits.is_sleepwear(product)


def _role_of(product: dict) -> str:
    """The part this piece plays, read from the shop rather than from a list.

    The merchant's own taxonomy first, then what the model worked out about this
    shop's product types, and only then the name. Nothing here is a table of
    garments someone typed in.
    """
    if part := parts.from_taxonomy(product.get("taxonomy")):
        return part
    if part := parts.known(product.get("category")):
        return part
    return "Other"


def _typical_shoe_eu(age: float | None) -> int | None:
    """The size chart's typical EU shoe size at this age (a fact, not a pick)."""
    if not age:
        return None
    from app.services import size_finder
    label = f"{round(age * 12)}M" if age < 2 else f"{int(age)}Y"
    i = size_finder.INDEX.get(label)
    if i is None:
        i = size_finder._clamp(size_finder._label_index(round(age * 12), "M") if age < 2
                               else size_finder._label_index(int(age), "Y"))
    return size_finder.CHART[i].shoe_eu


def _outgrown(piece: dict, age: float | None) -> bool:
    """Whether even this piece's largest size is below the child: a 4Y-at-most
    pair of trousers, or 26EU-at-most shoes, for a 6 year old. Read from the
    piece's own sizes against the size chart - a fact for the agent to act on."""
    if not age:
        return False
    sizes = [str(x) for x in piece.get("sizes") or []]
    ages = [a for x in sizes if (a := _age_of(x)) is not None]
    if ages:
        return max(ages) < int(age)
    numbers = _numbers_in(sizes)
    usual = _typical_shoe_eu(age)
    if numbers and usual and len(numbers) == len(sizes):
        return max(numbers) < usual - 1
    return False


def _age_rung(age: float | None) -> int | None:
    """A child's age on the shop's size ladder (the rung its own label would sit on)."""
    from app.services.size_finder import span_of
    if age is None:
        return None
    span = span_of(f"{int(age)}Y") if age >= 1 else span_of(f"{max(0, round(age * 12))}M")
    return span[0] if span else None


def _either_side(sizes: list[str], age: float | None) -> list[str]:
    """The piece's own sizes nearest this child: the one at or just under their
    age and the one at or just over it ("8Y", "10Y" for a 9 year old). Facts
    about the piece, for the agent to offer; one size if it is made in theirs."""
    from app.services.size_finder import span_of
    rung = _age_rung(age)
    laddered = [(span_of(s), s) for s in sizes or []]
    laddered = [(sp, s) for sp, s in laddered if sp]
    if rung is None or not laddered:
        return []
    exact = [s for sp, s in laddered if sp[0] <= rung <= sp[1]]
    if exact:
        return exact[:1]
    under = max(((sp, s) for sp, s in laddered if sp[1] < rung), default=None)
    over = min(((sp, s) for sp, s in laddered if sp[0] > rung), default=None)
    return [pick[1] for pick in (under, over) if pick is not None]


def _fits_age(sizes: list[str], age: int | None) -> bool:
    """Whether a piece is made for a child this old: its size range - smallest
    size to largest - covers their age. A dress in 8Y and 10Y suits a 9 year old;
    matching "9Y" as text threw it out. Shoe sizes and one-size say no age."""
    from app.services.size_finder import span_of
    if age is None or not sizes:
        return True
    spans = [span_of(s) for s in sizes]
    if any(sp is None for sp in spans):
        return True                     # shoe sizes / one size: not an age
    rung = _age_rung(age)
    if rung is None:
        return True
    return min(sp[0] for sp in spans) <= rung <= max(sp[1] for sp in spans)


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
                         category: str = "", limit: int = SUGGESTION_LIMIT,
                         min_price: float | None = None, avoid_colour: str = "",
                         exclude: list[str] | None = None) -> dict:
    """The in-stock pieces that suit what the shopper has said so far.

    Every filter is optional, so the first message of a conversation already
    gets something to look at. Filters are facts only - stock, this child,
    their age, budget, a kind they named. Which pieces answer the shopper, or
    make a look, is the agent's judgement from what each piece says it is.

    An occasion ranks rather than filters: the pieces whose own name, tags or
    description place them at that occasion come first, a neighbouring occasion
    next, and the rest after - so there is always something to show.
    """
    from app.services import shopper_identity as identity

    # In the shopper's own money: their budget is in it, and a ₹5000 budget
    # compared with base-currency prices let every $23 piece through.
    from app.services import market
    catalogue = await market.localize(await browse_catalogue())
    audience = _WHO.get((for_who or "").strip().lower()) or identity.shopping_for()
    # The agent does not always pass on what the shopper said; the preference
    # is held for the turn either way, so a suggestion never loses it.
    wanted = (colour or identity.wants_colour() or "").strip().lower()
    # int() turned "3 months" (0.25) into no age at all. Whole years stay whole.
    age = (int(age) if float(age) == int(age) else float(age)) if age else None
    # Like the colour above: what they told us holds even when it is not passed on.
    if age is None:
        age = identity.child_age()

    pool = [p for p in catalogue["products"] if p["in_stock"]]
    pool = _for_this_child(pool, audience)
    season = identity.shopping_season()
    # What each piece is FOR, read once and remembered. Only the pieces that
    # survive the cheap filters are read, so a shop of thousands costs no more
    # than the shelf a shopper is actually looking at.
    # Read, not filtered: each piece says what it is worn for and when, and the
    # agent judges. Word lists here once dropped a velvet summer-wedding dress.
    await suits.learn(pool)
    if age is not None:
        pool = [p for p in pool if _fits_age(p["sizes"], age)]
    if budget:
        pool = [p for p in pool if p["price_from"] is not None and p["price_from"] <= budget]
    # "Something more expensive" than the piece they were shown: a floor, in the
    # same money as the prices (localised above).
    if min_price:
        pool = [p for p in pool if p["price_from"] is not None and p["price_from"] > min_price]

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
        # Their colour first, the rest after - never dropped. Keeping only the
        # one blue dress made the agent say "the only dress in 5Y" when there
        # were nine; each piece says whether it comes in the colour, and the
        # agent decides what to show.
        if coloured:
            pool = coloured + [p for p in pool if p not in coloured]

    # Best fit first: the occasion the store's words actually place it at, then
    # a piece tagged for this child over one that merely suits either.
    # Pieces they have already seen and turned down ("I don't like these"): left
    # out, by handle or title, so the next set is genuinely new.
    if exclude:
        pool = [p for p in pool if not turned_down(p, exclude)]

    # A colour they turned down ("she doesn't like pink"): pieces that only come
    # in it go to the back and say so - kept, so the agent can still explain.
    avoid = (avoid_colour or "").strip().lower()

    def only_in_avoided(p: dict) -> bool:
        return bool(avoid) and bool(p["colors"]) and all(avoid in c.lower() for c in p["colors"])

    # Their colour leads and a turned-down one trails, then best fit: the
    # occasion the store's words place it at, a piece tagged for this child.
    pool.sort(key=lambda p: (
        # A piece the child has outgrown never takes a part of the look while
        # one that fits exists (26EU baby shoes left a 5 year old with no shoes).
        1 if _outgrown(p, age) else 0,
        1 if only_in_avoided(p) else 0,
        0 if not wanted or _colour_match(p["colors"], wanted) else 1,
        -_occasion_score(p, occasion),
        _season_first(p, season),
        0 if audience and audience in p["for"] else 1,
        p["price_from"] or 0,
    ))

    if wanted_category:
        # A kind was asked for: every piece of it that suits, best first - the
        # agent chooses what to show, not a cut-off of four.
        picked = pool[:limit] if limit else pool
    else:
        # Everything that suits this child, best fit first: which pieces make the
        # answer - or a look - is the agent's call, not one-per-part in code.
        picked = pool[:limit] if limit else pool

    # Age decides the sizes, so a look waits for it. A budget only narrows the
    # choice: without one the look is built anyway and they can give one after.
    still_to_ask = [] if age else ["age"]
    return {
        "currency": catalogue["currency"],
        "known": {"for": audience, "colour": colour or None, "occasion": occasion or None,
                  "age": age, "budget": budget or None, "category": wanted_category},
        "category_note": category_note,
        # Said whenever the child is older than our range for them, not only when
        # nothing came back: a 12 year old was handed one pair of plimsolls and
        # a question, with the 10Y shirts and chinos never in front of the agent.
        "nothing_else_fits": _range_note(catalogue["products"], audience, age),
        "occasion_matched": bool(occasion) and any(
            _occasion_score(p, occasion) >= 2 for p in picked),
        "colour_matched": colour_matched,
        "still_to_ask": still_to_ask,
        "budget_optional": not budget,
        # A shoe is sized by number, not age: the size chart's typical EU size for
        # a child this old, so a shoe in the look is not a guess.
        "typical_shoe_eu_for_age": _typical_shoe_eu(age),
        "count": len(picked),
        "products": [
            {
                "handle": p["handle"],
                "product_id": p["product_id"],
                "title": p["title"],
                "category": p["category"],
                "worn_for": ", ".join(suits.occasions_of(p)) or None,
                "seasons": ", ".join(suits.seasons_of(p)) or None,
                "about": suits.about_of(p),
                "part": _role_of(p),
                "price_from": p["price_from"],
                "currency": catalogue["currency"],
                "colour": _colour_match(p["colors"], wanted) if wanted else None,
                "only_in_colour_they_dislike": only_in_avoided(p) or None,
                "too_small_for_them": _outgrown(p, age) or None,
                # Made for their age (its range covers it), and which of its own
                # sizes sit either side - "8Y, 10Y" for a 9 year old.
                "for_their_age": True if age is not None else None,
                "sizes_either_side_of_their_age": _either_side(p["sizes"], age) or None,
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


def _colour_words(piece: dict) -> set[str]:
    """Every colour word this piece answers to, its name included.

    The store writes "Blue Denim" and "Baby Pink"; a shopper says "blue" and
    "pink". Matching whole strings missed both.
    """
    words: set[str] = set()
    for value in list(piece.get("colors") or []) + [piece.get("title") or ""]:
        words |= {w for w in re.findall(r"[a-z]+", str(value).lower()) if len(w) > 2}
    return words


def _name_forms(text: str) -> set[str]:
    """A product name as the agent may write it: with or without the store's
    size range - "George Check Shirt in Blue (12mths- 10yrs)" is the same shirt
    without its brackets, and exact matching let it come straight back."""
    whole = " ".join(str(text or "").lower().split())
    bare = re.sub(r"\s*\([^)]*\)\s*$", "", whole).strip()
    return {whole, bare} - {""}


def turned_down(piece: dict, names: list[str] | None) -> bool:
    """Whether this piece is one of the names they have already seen and turned down."""
    seen = set().union(*(_name_forms(n) for n in names or [])) if names else set()
    return bool(seen) and bool((_name_forms(piece.get("title") or "") | {str(piece.get("handle") or "").lower()}) & seen)


def _comes_in(piece: dict, colour: str | None) -> bool:
    """Whether the shopper could actually have this piece in the colour they asked for."""
    if not colour:
        return False
    wanted = {w for w in re.findall(r"[a-z]+", colour.lower()) if len(w) > 2}
    return bool(wanted & _colour_words(piece))



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
    told = [s for s in (piece.get("sizes") or []) if re.search(r"\d\s*[MY]\b", s.strip().upper())]
    return _fits_age(told, age) if told else True



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
    """The run of shoe sizes the shop sells, smallest to largest.

    Shoes only. A belt sold in "m-70-cm" and a hat in "56cm" are numbers too,
    and letting them in stretched the run to 80 - which put a six year old in
    the largest shoe in the shop.
    """
    numbers = [
        n for p in products
        if (p.get("role") or _category(p.get("title") or "", None)) == "Shoes"
        for n in _numbers_in([x for x in (p.get("sizes") or [])
                              if not re.search(r"\d\s*[MY]\b", x.strip().upper())
                              and "cm" not in x.lower()])
    ]
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
    # A belt in "S / 60cm" or a hat in "56cm" is measured, not shoe-sized: read
    # as a shoe, every belt in the shop was too big for a five year old.
    sizes = [x for x in (piece.get("sizes") or [])
             if not re.search(r"\d\s*[MY]\b", x.strip().upper()) and "cm" not in x.lower()]
    numbers = _numbers_in(sizes)
    if not numbers or len(sizes) != len(piece.get("sizes") or []):
        return True                     # sized by age, or not sized at all
    low, high = span
    target = low + (min(age, oldest) / oldest) * (high - low)
    return min(numbers) - 1 <= target <= max(numbers) + 1


def _range_note(stock: list[dict], audience: str | None, age: int | None) -> dict | None:
    """Told nothing fits, say why: the range for this child stops earlier.

    The boys' pieces run to 10Y, so a twelve year old boy has nothing here at
    all. "The look came back with just the plimsolls" is true and useless; "our
    boys' pieces go up to 10Y" is what a shopper needs to hear.
    """
    if age is None:
        return None
    theirs = [p for p in stock if not audience or not p["for"] or audience in p["for"]]
    oldest = max((a for p in theirs for x in (p["sizes"] or []) if (a := _age_of(x)) is not None),
                 default=None)
    if oldest is None or oldest >= age:
        return None
    found = {"asked_for_age": age, "oldest_we_make": oldest, "for": audience}
    # How big that largest size is, from the size chart - so whether it could
    # still fit this child is judged from a measurement, not from the label.
    from app.services.size_finder import CHART, INDEX
    band = INDEX.get(f"{oldest}Y")
    if band is not None:
        found["largest_cut_for_height_cm"] = CHART[band].height_cm
        found["largest_cut_for_chest_cm"] = CHART[band].chest_cm
    return found


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
        if age is None:
            return told[0]
        fits = [x for x in told if _fits_age([x], age)]
        if fits:
            return fits[0]
        # No size for exactly this age - the Lillie dress runs 6Y then 8Y, and
        # taking the first in the list put a seven year old in 18M. The nearest
        # age wins, and a tie goes to the larger: children grow into a size,
        # never out of one backwards.
        return sorted(told, key=lambda x: (abs((_age_of(x) if _age_of(x) is not None else 99) - age),
                                           -(_age_of(x) or 0)))[0]
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


_STYLIST = """You are the senior stylist of a children's clothing shop. Put together ONE complete look
around the anchor piece, choosing ONLY from the candidates given (every one is in stock, in
this child's size and for this child). In this shop a complete look is one the child can wear out of the door: dressed top to toe,
shoes included, and then finished. You decide, as a professional stylist would:
- which parts this look needs, for this child, occasion and season, and which pieces suit the
  anchor and each other - cut, formality, colour, texture;
- the finishing touches that make it a look rather than a list of clothes;
- for each piece, which of its listed colours to use;
- with a budget: the whole look INCLUDING the anchor must stay within it. Choose what to keep
  and what to leave out as a stylist would, and spend the budget well rather than leaving
  much of it unused. List in left_out the best piece the budget kept out, if any.
look_on_screen, when given, is the look they already have open: they are refining it. Keep each
of its pieces that still suits with what they have just said, and change only what that calls
for.
Use only handles and colours exactly as given. Return ONLY JSON:
{"pieces": [{"handle": "...", "colour": "..."}], "why": "one short line on why it works",
 "left_out": [{"handle": "...", "why": "what it would add"}]}"""

STYLIST_TIMEOUT = 30


async def _stylist(anchor: dict, pool: list[dict], *, age, size, audience, budget, currency,
                   wanted_colour, season, most: int | None = None) -> dict | None:
    """The look around `anchor`, chosen by the model from pieces that fit.

    Code only says what CAN go in - in stock, the child's size, for this child.
    What SHOULD go in is the model's call. None when it cannot be reached or
    gives nothing usable, so the caller falls back to its own picking.
    """
    if not pool:
        return None
    from app.agent.base import build_llm
    from app.services import shopper_identity as identity

    by_handle = {p["handle"]: p for p in pool}

    def colours_in_size(p: dict) -> list[str]:
        real = [c for c in p.get("combinations") or [] if c["available"] and c["color"]]
        fits = [c["color"] for c in real if not c["size"] or _same_size({"sizes": [c["size"]]}, size)]
        return sorted(set(fits or [c["color"] for c in real])) or p["colors"]

    await suits.learn(pool)
    candidates = [{"handle": p["handle"], "title": p["title"],
                   "worn_for": suits.occasions_of(p) or None, "seasons": suits.seasons_of(p) or None,
                   "about": suits.about_of(p),
                   "part": p.get("role") or _category(p["title"], None), "kind": p["category"],
                   "colours": colours_in_size(p), "price": p["price_from"]} for p in pool]
    brief = {
        "anchor": {"title": anchor["title"], "part": anchor.get("role") or _category(anchor["title"], None),
                   "colours": anchor["colors"], "price": anchor["price_from"]},
        "child": {"for": audience, "age": age, "size": size},
        "what_the_shopper_said": list(identity.said_messages())[-8:],
        "look_on_screen": list(identity.look_on_screen()),
        "wants_colour": wanted_colour, "avoids_colours": list(identity.avoids_colour()),
        "season": season, "budget": budget, "currency": currency,
        # Only where a panel has room for so many, never a styling rule.
        **({"at_most_pieces": most} if most else {}),
        "candidates": candidates,
    }
    ask = [("system", _STYLIST), ("human", json.dumps(brief, ensure_ascii=False))]
    for _ in range(2):
        try:
            # callbacks=[]: inside a shopper's turn, the JSON must not stream to them.
            answer = await asyncio.wait_for(build_llm(temperature=0.3, max_tokens=700).ainvoke(
                ask, config={"callbacks": [], "tags": ["stylist"], "run_name": "stylist"}),
                timeout=STYLIST_TIMEOUT)
            text = answer.content if isinstance(answer.content, str) else str(answer.content)
            found = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        except Exception:  # noqa: BLE001 - the caller still builds a look
            logger.warning("The stylist could not put a look together", exc_info=True)
            return None
        pieces, colours = [], {}
        for item in found.get("pieces") or []:
            piece = by_handle.get(str((item or {}).get("handle")))
            if piece and piece not in pieces:
                pieces.append(piece)
                if item.get("colour"):
                    colours[piece["handle"]] = str(item["colour"])
        if not pieces:
            return None
        total = (anchor["price_from"] or 0) + sum(p["price_from"] or 0 for p in pieces)
        if budget and total > float(budget):
            # A sum is not a judgement: say it, and let the stylist choose again.
            ask += [("ai", text), ("human", f"Those come to {total:.0f} {currency} with the anchor, over the "
                                            f"{budget:.0f} budget. Choose again within it.")]
            continue
        left_out = [{"title": by_handle[h]["title"], "price": by_handle[h]["price_from"],
                     "why": str(x.get("why") or "")[:200]}
                    for x in found.get("left_out") or []
                    if (h := str((x or {}).get("handle"))) in by_handle and by_handle[h] not in pieces]
        return {"pieces": pieces[:MAX_OUTFIT_ITEMS - 1], "colours": colours,
                "why": str(found.get("why") or "")[:300], "left_out": left_out[:2]}
    return None


async def complete_the_look(product: str, size: str | None = None,
                            budget: float | None = None, pieces: int | None = None) -> dict:
    """The coordinated outfit around the piece a shopper is looking at.

    One companion per category - a cardigan, shoes, an accessory - in stock, for
    the same child, in their size and sitting with the colours; then the whole
    look is priced through build_outfit so every line is a real variant the
    storefront can add to the bag.
    """
    # In the shopper's own money: their budget is in it, and a ₹5000 budget
    # compared with base-currency prices let every $23 piece through.
    from app.services import market
    catalogue = await market.localize(await browse_catalogue())
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
    # Nobody has said how old the child is, and this piece is sold across
    # several ages. The look is still shown - that is what they asked to see -
    # but every piece sold in several sizes is left for them to size: a guess
    # once dressed a shopper's daughter as a toddler without saying so.
    sizes_unknown = False
    if not (size or identity.wants_size()):
        spread = sorted({a for x in (anchor["sizes"] or []) if (a := _age_of(x)) is not None}) \
            or sorted(set(_numbers_in(anchor["sizes"] or [])))
        sizes_unknown = len(spread) > 1

    size = size or identity.wants_size() or next((s for s in anchor["sizes"] if s), None)
    ages = [a for p in stock for x in (p["sizes"] or []) if (a := _age_of(x))]
    oldest = max(ages) if ages else None
    # A shoe size is a number and says nothing about age, so ask the
    # conversation before giving up on knowing how old the child is.
    age = _age_of(size)
    if age is None:
        age = _age_of(identity.wants_size())   # nought is an age: a baby is 0
    if age is None:
        # Nobody has said how old the child is, and the piece runs from 18 months
        # to twelve years. Taking its first size dressed everyone as a baby and
        # its numbers made a plimsoll a 20; the middle of what this piece is sold
        # in is the one guess that keeps the whole look the same size.
        ladder = sorted(a for x in (anchor["sizes"] or []) if (a := _age_of(x)))
        if ladder:
            age = ladder[len(ladder) // 2]
            # The size was only the piece's first, not the shopper's: keeping it
            # held the anchor at 18M while everything round it came out 7Y.
            size = next((x for x in anchor["sizes"] if _age_of(x) == age), None)
        else:
            numbers = sorted(_numbers_in(anchor["sizes"] or []))
            run = _shoe_span(stock)
            if numbers and run and run[1] > run[0] and oldest:
                middle = numbers[len(numbers) // 2]
                age = max(0, round(oldest * (middle - run[0]) / (run[1] - run[0])))
                size = None      # a shoe number is not a size for the clothes

    # Every other piece is a candidate - a cardigan over a shirt is the
    # stylist's call. The fallback picking below never takes the anchor's role.
    pool = [p for p in stock if p["handle"] != anchor["handle"]]
    pool = _for_this_child(pool, audience)
    span = _shoe_span(stock)
    season = identity.shopping_season()
    pool = [p for p in pool if _same_size(p, size) and _suits_age(p, age)
            and _numbered_size_fits(p, age, oldest, span)]
    if budget:
        pool = [p for p in pool if (p["price_from"] or 0) <= budget]

    # The look itself is a stylist's judgement, so the model makes it from the
    # pieces that really fit this child; the loops below only run when it
    # cannot be reached.
    styled = await _stylist(anchor, pool, age=age, size=size, audience=audience, budget=budget,
                            currency=catalogue.get("currency"), wanted_colour=wanted_colour,
                            season=season, most=pieces)
    # The look is the stylist's call alone: no rule-picked stand-in when it
    # cannot answer - the agent says so and offers to try again.
    if pool and styled is None:
        return {"found": False, "asked_for": product, "reason": "stylist_unavailable",
                "tell_customer": "The look could not be put together just now - say so and offer to try again."}
    picked = list(styled["pieces"]) if styled else []
    chosen_colour = styled["colours"] if styled else {}

    def line(piece: dict) -> dict:
        """One line of the look: a colour and size the shop sells together.

        Picking the colour and the size apart asked for pairs that do not
        exist - a raspberry plimsoll in a size only the navy comes in - and
        the piece was quietly dropped from the look it was meant to anchor.
        """
        item = {"handle": piece["handle"], "quantity": 1}
        real = [c for c in (piece.get("combinations") or []) if c["available"]]
        if not real:
            if piece["colors"]:
                item["color"] = piece["colors"][0]
            if piece["sizes"]:
                item["size"] = _size_for_age(piece["sizes"], age, oldest, span)
            return item

        # The size comes first - it has to fit the child - and the colour then
        # chooses among what is left. Doing it the other way round picked navy,
        # which this shoe only comes in large, and put a 30 on a baby.
        choices = real
        sizes = [c["size"] for c in real if c["size"]]
        if sizes:
            numbered = not any(re.search(r"\d\s*[MY]\b", x.strip().upper()) for x in sizes)
            fits = sizes if numbered else [x for x in sizes if _same_size({"sizes": [x]}, size)]
            wanted_size = _size_for_age(fits or sizes, age, oldest, span)
            choices = [c for c in real if c["size"] == wanted_size] or real

        # The stylist's colour where this size comes in it, else the one they asked for.
        theirs = [c for c in choices if (c["color"] or "") == chosen_colour.get(piece["handle"])]
        if not theirs:
            theirs = [c for c in choices if _comes_in({"colors": [c["color"] or ""]}, wanted_colour)]
        picked_one = (theirs or choices)[0]
        if picked_one["color"]:
            item["color"] = picked_one["color"]
        if picked_one["size"]:
            item["size"] = picked_one["size"]
        return item

    look = await build_outfit([line(anchor), *[line(p) for p in picked]], budget)
    if not picked and (note := _range_note(stock, audience, age)):
        look["nothing_else_fits"] = note
    if sizes_unknown:
        sized = {p["handle"]: len(p.get("sizes") or []) for p in stock}
        open_rows = []
        for item in look.get("outfit") or []:
            if sized.get(item.get("handle"), 0) > 1:
                # The variant stays only so the price can be quoted in the
                # shopper's money; "needs" keeps it out of the bag until they size it.
                item["option"] = None
                item["needs"] = ["size"]
                open_rows.append(item["title"])
        look["cart_items"] = [{"variant_id": None if c.get("needs") else c["variant_id"], "quantity": c["quantity"]}
                              for c in look.get("outfit") or []]
        if open_rows:
            look["sizes_to_choose"] = open_rows
            look["ask"] = "how old the child is"
    look["found"] = bool(look.get("outfit"))
    look["anchor"] = {"handle": anchor["handle"], "title": anchor["title"], "category": anchor["category"]}
    look["size"] = size
    look["heading"] = f"The coordinated look around the {anchor['title']}"
    if styled:
        look["stylist_note"] = styled["why"]
        if styled["left_out"]:
            look["left_out_for_budget"] = styled["left_out"]
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
                "taxonomy": (node.get("category") or {}).get("fullName"),
                "season": ((node.get("season") or {}).get("value") or None),
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
    if not candidates and want_size:
        # The shop's own size names are long: "28" is "10UK/11US/28EU", "5Y" is
        # "5/6Y". Taken only when exactly one size answers to it.
        from app.services.extras import _size_matches
        loose = [v for v in product["variants"]["nodes"]
                 if (not want_colour or (_colour_of(v) or "").casefold() == want_colour.casefold())
                 and _size_matches(want_size, [_option_value(v, "Size") or ""])]
        if len({_option_value(v, "Size") for v in loose}) == 1:
            candidates = loose
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
    left_out: list[dict] = []
    total = Decimal("0")

    # The pieces here were chosen by the agent, so the facts are checked again:
    # whose look it is, and that its sizes are made for a child this old.
    from app.services import shopper_identity as identity

    for_whom = identity.shopping_for()
    how_old = _age_of(identity.wants_size())
    if how_old is None:
        told = [a for i in requested if (a := _age_of(str(i.get("size") or ""))) is not None]
        how_old = told[0] if told else None

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

        piece = {"title": product["title"],
                 "category": product.get("productType"),
                 "for": audience_reader.of(product.get("tags"), product.get("legacyResourceId")),
                 "sizes": _options_of(product).get("Size") or []}
        if for_whom and not _for_this_child([piece], for_whom):
            left_out.append({"title": product["title"], "reason": "for_another_child",
                             "shopping_for": for_whom})
            continue
        # The agent may knowingly choose the nearest size a piece is sold in -
        # a 12 year old where the boys' range stops at 10Y. That choice stands;
        # a piece whose sizes are all for another age is still kept out. What
        # goes in the look (two tops, a cardigan over a shirt) is the agent's call.
        sold_in_asked = bool(size) and any(str(size).strip().lower() == str(x).strip().lower()
                                           for x in piece["sizes"])
        if not sold_in_asked and not _suits_age(piece, how_old):
            left_out.append({"title": product["title"], "reason": "not_made_for_this_age",
                             "age": how_old})
            continue

        # No size given for a piece sold in several: that is the shopper's (or
        # the agent's, from their age) choice to make - never the first in stock.
        sizes_sold = _options_of(product).get("Size") or []
        if not size and len(sizes_sold) > 1:
            problems.append({"handle": handle, "title": product["title"], "reason": "size_needed",
                             "available_sizes": sizes_sold})
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
        "left_out": left_out,
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
