"""Follow-on prompts a shopper can tap instead of typing.

Every suggestion is built from something the store actually stocks, and the text
sent on a tap is a phrasing the agent already resolves. That is the whole point:
a chip the model invented could read beautifully - "browse our dining range" -
and then land on nothing, which is worse than offering no chip at all.

Order is deliberate. What the shopper is looking at leads, the rest of the shop
follows, and the catch-all sits last where it costs nothing.
"""

import logging
import re

from app.services import shopify_storefront

logger = logging.getLogger(__name__)

MAX_SUGGESTIONS = 4
CATEGORY_POOL = 12       # categories considered before picking
BEST_SELLERS = {"label": "Show best sellers", "prompt": "What are your best sellers?"}

# Plurals the -s rule gets wrong, and names that are plural already.
_IRREGULAR = {"dress": "dresses", "blouse": "blouses", "bib": "bibs", "hairband": "hairbands"}
_ALREADY_PLURAL = {"shoes", "boots", "trousers", "shorts", "sunglasses", "mittens", "socks",
                   "toys", "plants", "essentials", "aminos"}


# ── Answering our own question ─────────────────────────────────────────────
# When the agent asks the shopper something, a row of shelves is the wrong
# answer: they have just been asked what the occasion is, and "Explore Dresses"
# does not answer it. These chips reply to the question that was actually put.
#
# Each prompt is a sentence the agent already handles in the outfit flow, so a
# tap moves the conversation on rather than restarting it.

OCCASION_CHIPS = [
    {"label": "Birthday party", "prompt": "It is for a birthday party", "kind": "occasion"},
    {"label": "Wedding", "prompt": "It is for a wedding", "kind": "occasion"},
    {"label": "Christening", "prompt": "It is for a christening", "kind": "occasion"},
    {"label": "Everyday", "prompt": "Just for everyday wear", "kind": "occasion"},
]

WHO_CHIPS = [
    {"label": "For a girl", "prompt": "It is for a girl", "kind": "who"},
    {"label": "For a boy", "prompt": "It is for a boy", "kind": "who"},
]

# Ages spread across what the store sizes for, 0-3M up to 10Y. A shopper whose
# child is 4 types it; a chip for every year would bury the row.
AGE_CHIPS = [
    {"label": "Under 1", "prompt": "They are under 1", "kind": "age"},
    {"label": "3 years", "prompt": "They are 3 years old", "kind": "age"},
    {"label": "5 years", "prompt": "They are 5 years old", "kind": "age"},
    {"label": "8 years", "prompt": "They are 8 years old", "kind": "age"},
]

# No "no fixed budget" chip on purpose: with no number the look is never
# priced, budget stays in still_to_ask, and the agent asks for it again.
BUDGET_CHIPS = [
    {"label": "Under 10,000", "prompt": "My budget is 10000", "kind": "budget"},
    {"label": "Under 20,000", "prompt": "My budget is 20000", "kind": "budget"},
    {"label": "Under 30,000", "prompt": "My budget is 30000", "kind": "budget"},
]

# Asked in priority order: a compound question ("boy or girl, what occasion,
# which colour?") can only be answered one chip at a time, so the row answers
# the most useful part and the shopper types or taps the rest.
_QUESTION_KINDS = [
    ("occasion", re.compile(r"occasion|what is it for|what's it for|dressing up for", re.I)),
    ("who", re.compile(r"boy or (?:a )?girl|girl or (?:a )?boy|who is it for|who's it for|who are you shopping for", re.I)),
    ("age", re.compile(r"how old|what age|\bage\b", re.I)),
    ("budget", re.compile(r"budget|how much (?:would|do|can) you|spend", re.I)),
    ("colour", re.compile(r"colou?r", re.I)),
]


def _asks_a_question(reply: str) -> bool:
    return "?" in reply


_QUESTION_RE = re.compile(r"[^.!?\n]*\?")


def _questions_in(reply: str) -> str:
    """Only the sentences that ask something.

    The rest of a reply describes, and describing is not asking: "here is a look
    under your 20,000 budget - want me to add it to the bag?" asks about the bag,
    and matching the whole reply put budget chips under it.
    """
    return " ".join(_QUESTION_RE.findall(reply or ""))


def question_chips(reply: str, colours: list[str] | None = None) -> list[dict]:
    """Chips answering whatever the reply asked, or [] if it asked nothing."""
    if not reply or not _asks_a_question(reply):
        return []
    asked = _questions_in(reply)
    for kind, pattern in _QUESTION_KINDS:
        if not pattern.search(asked):
            continue
        if kind == "occasion":
            return [dict(c) for c in OCCASION_CHIPS]
        if kind == "who":
            return [dict(c) for c in WHO_CHIPS]
        if kind == "age":
            return [dict(c) for c in AGE_CHIPS]
        if kind == "budget":
            return [dict(c) for c in BUDGET_CHIPS]
        if colours:
            return [{"label": c, "prompt": f"In {c.lower()}", "kind": "colour"} for c in colours]
    return []


async def _stocked_colours(limit: int = 4) -> list[str]:
    """The colours the shop actually has most of, so a chip cannot miss."""
    from app.services import outfit

    try:
        catalogue = await outfit.browse_catalogue()
    except Exception:  # noqa: BLE001 - a chip row is never worth failing a reply for
        logger.warning("Could not read colours for suggestions", exc_info=True)
        return []
    counts: dict[str, int] = {}
    for product in catalogue.get("products") or []:
        for colour in product.get("colors") or []:
            counts[colour] = counts.get(colour, 0) + 1
    return sorted(counts, key=lambda c: -counts[c])[:limit]


def plural(name: str) -> str:
    """"Dress" -> "Dresses", "Shoes" -> "Shoes". Chips read as a shelf, not a SKU."""
    lowered = name.strip().lower()
    if lowered in _ALREADY_PLURAL:
        return name
    if lowered in _IRREGULAR:
        return _IRREGULAR[lowered].title()
    if lowered.endswith(("ss", "x", "ch", "sh")):
        return f"{name}es"
    # Already plural - "Dresses", "T-Shirts". Adding to it gave "Explore Dresseses".
    if lowered.endswith("s"):
        return name
    return f"{name}s"


def _category_chip(entry: dict) -> dict:
    name = plural(entry["name"])
    return {"label": f"Explore {name}", "prompt": f"Show me {name.lower()}",
            "kind": "category", "id": entry.get("id")}


def _collection_chip(entry: dict) -> dict:
    title = entry.get("name") or entry.get("title") or ""
    return {"label": f"Shop {title}", "prompt": f"What is in {title}?",
            "kind": "collection", "id": entry.get("handle") or entry.get("id")}


# ── Where the shopper is ───────────────────────────────────────────────────
# The general shelves are the same whatever was just said - ask about Baby
# Accessories & Gifts and the row still offered Dresses and Shirts. So the row
# starts from where the shopper is: the shelves of the products this reply
# showed, then their neighbours under the same broad collection. A bib leads to
# Bibs, Teddy Bears, Socks, Belts and Sunglasses.

CONTEXT_LIMIT = 5


def _explore_chip(card: dict) -> dict:
    title = card.get("title") or card.get("name") or ""
    return {"label": f"Explore {title}", "prompt": f"What is in {title}?",
            "kind": "collection", "id": card.get("handle") or card.get("id")}


async def _context_chips(shown_products: list[dict], shown_category: dict | None) -> list[dict]:
    """Shelves around what the shopper is looking at, nearest first, or []."""
    tree = await shopify_storefront.collection_tree()
    handles: list[str] = []

    def add(handle: str | None) -> None:
        if handle and handle in tree["cards"] and handle not in handles:
            handles.append(handle)

    def neighbours(product_type: str) -> list[str]:
        return tree["children"].get(tree["parent"].get(product_type), [])

    def shelf(product_type: str) -> str | None:
        # Romper has no collection of its own, only Bodysuits & Rompers.
        return tree["home"].get(product_type) or tree["parent"].get(product_type)

    # 1. The products this reply showed: each one's own shelf, then its neighbours.
    types: list[str] = []
    for product in shown_products:
        found = (tree["type_of"].get(str(product.get("product_id") or ""))
                 or tree["type_of"].get((product.get("title") or "").strip().lower()))
        if found and found not in types:
            types.append(found)
    for t in types:
        add(shelf(t))
    for t in types:
        for handle in neighbours(t):
            add(handle)

    # 2. A collection or category the shopper named: what sits under it, or beside it.
    standing_in = None
    if shown_category:
        ident = shown_category.get("id")
        if shown_category.get("kind") == "collection" and ident:
            standing_in = ident
            if ident in tree["children"]:
                for handle in tree["children"][ident]:
                    add(handle)
            elif ident in tree["collection_type"]:
                for handle in neighbours(tree["collection_type"][ident]):
                    add(handle)
        elif shown_category.get("name"):
            named = shown_category["name"]
            standing_in = tree["home"].get(named)
            for handle in neighbours(named):
                add(handle)

    # The shelf they are already standing on is not a suggestion.
    return [_explore_chip(tree["cards"][h]) for h in handles if h != standing_in][:CONTEXT_LIMIT]


async def for_turn(shown_category: dict | None = None, limit: int = MAX_SUGGESTIONS,
                   reply: str = "", shown_products: list[dict] | None = None) -> list[dict]:
    """Chips to offer after a reply, most relevant first.

    1. A reply that asks the shopper something gets chips that answer it.
    2. Otherwise the shelves around what they are looking at: the collections of
       the products this reply showed and their neighbours, or what sits under a
       collection they named. Up to five.
    3. Failing both, the general shelves.
    """
    answering = question_chips(reply)
    if not answering and reply and _asks_a_question(reply) and re.search(r"colou?r", _questions_in(reply), re.I):
        answering = question_chips(reply, await _stocked_colours(limit))
    if answering:
        return answering[:limit]

    # "Shall I show you our most popular pieces, or a category?" - the first
    # answer to that is the best-sellers chip, so it leads, and the categories
    # fill the rest in place of the collection.
    offers_best = bool(re.search(r"best.?sell|most popular|popular pieces|top (?:selection|pick)s?",
                                 _questions_in(reply), re.I))

    if not offers_best:
        try:
            context = await _context_chips(shown_products or [], shown_category)
        except Exception:  # noqa: BLE001 - chips are a nicety, never a reason to fail a reply
            logger.warning("Could not build context suggestions", exc_info=True)
            context = []
        if len(context) >= limit:
            return context[:CONTEXT_LIMIT]
        if context:
            # One shelf with no neighbours - Dresses - is topped up from the
            # general shelves rather than left as a single chip.
            labels = {c["label"] for c in context}
            filler = await _shelf_chips(shown_category, limit, offers_best=False)
            return (context + [c for c in filler if c["label"] not in labels])[:limit]

    return await _shelf_chips(shown_category, limit, offers_best)


async def _shelf_chips(shown_category: dict | None, limit: int, offers_best: bool) -> list[dict]:
    """The general shelves - the fullest categories, one collection, best sellers -
    for when the reply gives nothing to go on. The shelf the shopper is on is left
    out: offering it back is not a suggestion."""
    seen_id = (shown_category or {}).get("id")
    seen_name = ((shown_category or {}).get("name") or "").strip().lower()
    chips: list[dict] = [dict(BEST_SELLERS)] if offers_best else []

    try:
        listed = (await shopify_storefront.categories(CATEGORY_POOL))["categories"]
    except Exception:  # noqa: BLE001 - chips are a nicety, never a reason to fail a reply
        logger.warning("Could not build category suggestions", exc_info=True)
        listed = []

    for entry in listed:
        if len(chips) >= limit - (0 if offers_best else 1):
            break
        if entry.get("id") == seen_id or entry["name"].strip().lower() == seen_name:
            continue
        chip = _category_chip(entry)
        # "Dress" and "Dresses" are separate product types in the store but the
        # same shelf to a shopper - one "Explore Dresses" is enough.
        if any(c["label"] == chip["label"] for c in chips):
            continue
        chips.append(chip)

    # One collection alongside the plain categories: it is the merchandised door,
    # and it reads differently enough that the row does not look like one list.
    try:
        collections = (await shopify_storefront.collections(6))["collections"]
        for entry in collections:
            handle = entry.get("handle") or entry.get("id")
            if not offers_best and handle and handle != seen_id and len(chips) < limit:
                chips.append(_collection_chip(entry))
                break
    except Exception:  # noqa: BLE001
        logger.warning("Could not build a collection suggestion", exc_info=True)

    if not offers_best and len(chips) < limit:
        chips.append(dict(BEST_SELLERS))
    return chips[:limit]


async def for_welcome(limit: int = MAX_SUGGESTIONS) -> list[dict]:
    """Chips for the opening screen, where nothing has been shown yet."""
    return await for_turn(None, limit)
