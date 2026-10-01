"""Product cards for the chat clients.

A tool result is JSON meant for the model; a storefront needs a picture, a price
and somewhere to click. This turns one into the other, and is shared by every
chat endpoint so a client gets the same cards whether it streams the reply or
takes it in one piece.
"""

import json
import logging
import re

from app.services import needs
from app.services import shopper_identity as identity

# Tools whose result a client can render as cards, and the key it arrives under.
logger = logging.getLogger(__name__)

CARD_TOOLS = {
    "search_products": "products",
    "browse_category": "products",
    "browse_in_size": "products",

    "get_best_sellers": "products",
    "browse_catalogue": "products",
    "suggest_pieces": "products",
    "compare_products": "products",
    "recommend_for_me": "products",
    "show_saved_items": "products",
    "product_details": "products",
    "build_outfit": "outfit",
    "complete_the_look": "outfit",

    "show_size": "size",
    "get_my_order_history": "orders",
    "check_order_status": "orders",
    # Not a card - a set of buttons. Same idea though: the shopper should be
    # tapping a choice, not reading the agent recite seven of them.
    "request_order_change": "choices",
}
MAX_CARDS = 12
# A shelf - everything in 5Y, a whole category - opens on this many cards; the
# storefront fetches the rest from /support/more when the shopper asks.
SHELF_FIRST = 8
# Category tiles are the whole answer to "what categories do you have", so
# every one is drawn - the reply names them all and each must be tappable.
MAX_CATEGORY_TILES = 60

# Tools whose product list IS the answer, not a shortlist the agent then talks
# about. A category browse is the shopper's own request drawn back at them, so
# it is sent whole - trimming it to the few products the reply names would empty
# a grid the shopper explicitly asked to see.
WHOLE_RESULT_TOOLS = {"browse_category", "browse_in_size"}

# Tools whose products are never trimmed to the wording. A comparison is every
# product in it, whichever of them the reply happens to name in full.
FIXED_RESULT_TOOLS = {"compare_products", "show_saved_items", "product_details"}


_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that say nothing about which product this is. Held as stems, and matched
# after stemming, so "boys" and "boy" are both caught - previously only the
# plural was listed, and the singular in "is it for a boy or a girl?" picked out
# the Cream Boy's Belt and drew it under a question that named no product.
#
# The second group describes the shopper, not the garment. Those words turn up
# in every clarifying question we ask, so a title carrying one must not be
# recognised by it: "Leather T Bar Baby Shoes" is still found by "t bar".
_NOISE = {
    "the", "and", "for", "with", "in", "of", "a", "an",
    "kid", "girl", "boy", "child", "children", "baby", "toddler",
    "year", "old", "size", "colour", "color", "man", "men", "woman", "women",
}
# Used only when a title has no word of its own to be recognised by.
_MENTION_RATIO = 0.5
# A distinctive word this short ("all", "one", "set") is too common in plain
# prose to name a product on its own.
SHORT_WORD = 5


def _stem(word: str) -> str:
    """Fold simple plurals so "Mary Janes" still finds "Mary Jane"."""
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


# The colours the chat already recognises in what a shopper types.
_COLOUR_WORDS = {_stem(c) for c in needs._COLOURS}


def _words(text: str) -> set[str]:
    stems = (_stem(w) for w in _WORD_RE.findall(text.lower()) if len(w) > 2)
    return {s for s in stems if s not in _NOISE}


def keep_mentioned(items: list[dict], reply: str) -> list[dict]:
    """The products the reply actually talks about.

    Prose shortens titles - "Catherine Gingham Embroidered Sleeveless Trapeze
    Dress" becomes "the Catherine Gingham dress", "Leather Mary Jane Shoes"
    becomes "the Mary Janes" - so a product counts as mentioned when the reply
    uses a word that belongs to it alone.

    Matching on any shared word would be wrong in the other direction: with
    "Leather T Bar Baby Shoes" in the reply, "leather" and "shoes" must not drag
    the Mary Janes in beside it.
    """
    # A bulleted list is the agent's choice, line by line; the prose around it
    # ("6 of 17 pieces in his size") is commentary, and "pieces" there was
    # drawing the "Two Piece Set" card.
    bullets = [line for line in reply.splitlines() if re.match(r"\s*[-*\u2022]\s+\S", line)]
    if bullets:
        # Bullets that name no product (a list of features) leave the prose in charge.
        return _mentioned_in(items, "\n".join(bullets)) or _mentioned_in(items, reply)
    return _mentioned_in(items, reply)


def _mentioned_in(items: list[dict], reply: str) -> list[dict]:
    said = _words(reply)
    # Sentence by sentence, for the short words: "27 pieces in all" is not the
    # "All In One" - a short word only names a product beside another of its words.
    sentences = [_words(part) for part in re.split(r"[.!?\n]+", reply) if part.strip()]
    title_words = [(item, _words(item.get("title") or "")) for item in items]

    def named_by(word: str, words: set[str]) -> bool:
        if any(word in part and len(part & words) > 1 for part in sentences):
            return True
        # On its own a word names a product only when it is written as a name -
        # "the Alice", not "I'd need to check" beside the George Check shirt.
        # A colour never does: "comes in Navy and Cream" is not the Cream shorts.
        if len(word) < SHORT_WORD or word in _COLOUR_WORDS or word not in said:
            return False
        return bool(re.search(r"\b" + re.escape(word[:1].upper() + word[1:]), reply))

    frequency: dict[str, int] = {}
    for _, words in title_words:
        for word in words:
            frequency[word] = frequency.get(word, 0) + 1

    kept = []
    for item, words in title_words:
        if not words:
            continue
        distinctive = {w for w in words if frequency.get(w, 1) == 1}
        if distinctive:
            if any(named_by(w, words) for w in distinctive):
                kept.append(item)
        elif len(words & said) / len(words) >= _MENTION_RATIO:
            # Nothing sets this title apart, so fall back to how much of it appears.
            kept.append(item)
    return kept


def keep_orders_mentioned(orders: list[dict], reply: str) -> list[dict]:
    """The orders the reply actually talks about.

    "What was my last order?" is answered about one order, but the tool hands
    back the last five, and drawing all of them put four the shopper did not ask
    about under an answer about one.

    An order number is only counted when it is written as one - "#1033", or
    "order 1033". A bare 1033 is ignored on purpose: replies are full of prices
    and totals, and "1033.00" should not pull up order 1033.
    """
    kept = []
    for order in orders:
        bare = (order.get("order_number") or "").lstrip("#").strip()
        if not bare:
            continue
        num = re.escape(bare)
        if re.search(rf"#\s*{num}\b", reply) or re.search(rf"\border\s+#?{num}\b", reply, re.I):
            kept.append(order)
    return kept


_CHOICE_LINE_RE = re.compile(r"^\s*\d+[.)]\s")


def _without_choices(reply: str) -> str:
    """The reply minus the numbered choices the storefront turns into buttons.

    Those lines are the agent's own questions - "everyday, party, school" - and
    matching products against them drags in whatever happens to share a word with
    a menu option, like a Party Dress under a question about the occasion.
    """
    lines = reply.splitlines()
    while lines and (not lines[-1].strip() or _CHOICE_LINE_RE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines)


def _in_their_colour(item: dict) -> dict:
    """The variant to show first: the colour they asked for, if it comes in it.

    A shopper who said "blue" was shown the pink pair of sunglasses, because a
    card takes the product's first variant and the store lists pink first. The
    picture, the price and the pre-picked colour all follow the variant, so
    choosing the right one here fixes all three.
    """
    wanted = identity.wants_colour()
    variants = item.get("variants")
    if not wanted or not isinstance(variants, list):
        return item
    match = next((v for v in variants
                  if v.get("available") and wanted in str(v.get("option") or "").lower()), None)
    if not match:
        return item
    return {**item,
            "variant_id": match.get("variant_id") or item.get("variant_id"),
            "option": match.get("option") or item.get("option"),
            "image": match.get("image") or item.get("image"),
            "url": match.get("url") or item.get("url")}


def _card(item: dict) -> dict:
    """The fields a storefront needs to draw a product and link to it."""
    item = _in_their_colour(item)
    return {
        "product_id": item.get("product_id"),
        "variant_id": item.get("variant_id"),
        "title": item.get("title"),
        "option": item.get("option"),
        # The colour/size the shopper asked for, so the card opens with them picked.
        # A size lookup says which of a piece's sizes matched the one asked for
        # ("3 months" -> its "3M"); that is their size on this piece too.
        "chosen_options": item.get("chosen_options") or (
            item["in_this_size"] if isinstance(item.get("in_this_size"), list)
            and len(item["in_this_size"]) == 1 else None),
        # Tools name this differently: a unit price, a "from" price, or a plain one.
        "price": next(
            (item[k] for k in ("unit_price", "price_from", "price") if item.get(k) is not None),
            None,
        ),
        "currency": item.get("currency"),
        "image": item.get("image"),
        "url": item.get("url"),
        # Only recommendations set this; it is why the product was suggested.
        "because": item.get("because"),
    }


def cards_from(tool_name: str, output: str | None) -> dict | None:
    """Renderable cards from one tool result, or None when there are none."""
    if not output or tool_name not in CARD_TOOLS:
        return None
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None

    currency = data.get("currency")

    def card(item: dict) -> dict:
        return {**_card(item), "currency": item.get("currency") or currency}

    if tool_name in ("get_my_order_history", "check_order_status"):
        # check_order_status returns one order; the history tool returns a list.
        orders = data.get("orders") if "orders" in data else ([data] if data.get("found") else [])
        orders = [o for o in (orders or []) if o.get("order_number")]
        if not orders:
            return None
        return {
            "orders": [
                {
                    "order_number": o.get("order_number"),
                    "placed_on": o.get("placed_on"),
                    "status": o.get("status"),
                    "status_meaning": o.get("status_meaning"),
                    "total": o.get("total"),
                    "currency": o.get("currency"),
                    "tracking": o.get("tracking") or [],
                    "items": [
                        _card(i) | {"quantity": i.get("quantity"), "line_total": i.get("line_total")}
                        for i in (o.get("items") or [])
                    ],
                }
                for o in orders[:MAX_CARDS]
            ]
        }

    if tool_name == "choices" or tool_name == "request_order_change":
        if not data.get("eligible"):
            return None
        options = [
            {"code": r["code"], "label": r["label"]}
            for r in (data.get("reasons") or [])
            if r.get("code") and r.get("label")
        ]
        if not options:
            return None
        return {
            "options": options,
            "action": data.get("action"),
            "order_number": data.get("order_number"),
            # There is always a way out of the list.
            "allow_free_text": True,
        }

    if tool_name in ("build_outfit", "complete_the_look"):
        items = data.get("outfit") or []
        if not items:
            return None
        return {
            "items": [card(i) for i in items[:MAX_CARDS]],
            "currency": currency,
            "total": data.get("total"),
            "budget": data.get("budget"),
            "within_budget": data.get("within_budget"),
            "cart_items": data.get("cart_items") or [],
            "multi_buy": data.get("multi_buy"),
        }

    if tool_name == "show_size":
        # The quiz result as it stands: the size, why, and the product it is for.
        return data if data.get("found") else None

    if tool_name == "compare_products":
        items = data.get("products") or []
        if len(items) < 2:
            return None                     # one product is not a comparison
        # Ordinary product cards, so they render as they are today, carrying the
        # spec rows and highlights for a widget that wants to line them up.
        return {
            "items": [card(i) | {"specs": i.get("specs") or [], "highlights": i.get("highlights") or []}
                      for i in items],
            "currency": currency,
            "heading": data.get("heading"),
            "layout": "comparison",
            # Where they differ, one row per attribute, values in card order -
            # ready to lay out as a table beside the cards.
            "difference": data.get("difference") or [],
        }

    if tool_name == "product_details":
        return {"items": [card({**data, "price": data.get("price_from")})], "currency": currency} if data.get("found") else None

    items = data.get("products") or []
    if not items:
        return None
    # Every product stays for now. finalise() matches these against the reply and
    # as_dict() takes the cap: trimming here first meant a catalogue of fifty was
    # cut to twelve before anyone asked which ones the agent had named, so a
    # product mentioned from further down the list had no card to attach to.
    result = {"items": [card(i) for i in items], "currency": currency}
    # A title for the row, when the tool has one - "Picked for you: Dresses and
    # Cardigans" above a set of recommendations.
    if data.get("heading"):
        result["heading"] = data["heading"]
    # A shelf carries its full size and how to fetch the rest of it.
    if isinstance(data.get("shelf"), dict):
        result["shelf"] = data["shelf"]
        result["total"] = data.get("count") or len(items)
    return result


def _as_page(products: dict) -> dict:
    """A shelf's first page: SHELF_FIRST cards, the total, and how to get more.
    The shelf key goes when nothing is left to fetch."""
    items = products.get("items") or []
    page = {**products, "items": items[:SHELF_FIRST]}
    if (page.get("total") or 0) <= len(page["items"]):
        page.pop("shelf", None)
        page["total"] = len(page["items"])
    return page


# The agent's own word on which products to draw, as the last line of its reply:
# "[show: 9282580316316, 9227218190492]", "[show: all]" or "[show: none]".
_SHOW_RE = re.compile(r"\s*\[\s*show\s*:\s*([^\]]*)\]\s*$", re.I)


def split_show(reply: str) -> tuple[str, list[str] | str | None]:
    """The reply without its [show: ...] line, and what that line chose:
    a list of product ids, "all", or None when the agent gave no line."""
    m = _SHOW_RE.search(reply or "")
    if not m:
        return reply, None
    body = m.group(1).strip().lower()
    text = (reply[:m.start()]).rstrip()
    if body in ("all", "*"):
        return text, "all"
    if body in ("none", "", "-"):
        return text, []
    return text, [x for x in re.findall(r"\d{5,}", body)]


def _not_a_shelf(products: dict, items: list[dict]) -> dict:
    """The same row, cut down to what the reply chose - no longer a whole shelf."""
    out = {**products, "items": items}
    out.pop("shelf", None)
    out.pop("total", None)
    return out


class CardCollector:
    """Gathers cards across a turn so they can be sent mid-stream and at the end.

    A turn may call several tools; the last product result is the one the reply
    is actually about, so later cards replace earlier ones of the same kind.
    """

    def __init__(self) -> None:
        self.products: dict | None = None
        self.products_whole = False
        self.products_fixed = False
        self.products_tool: str | None = None
        # Every product result this turn, in the order they finished.
        self.product_results: list[tuple[str, dict]] = []
        # Instructions for the widget - add these variants, open checkout - in
        # the order the agent issued them.
        self.actions: list[dict] = []
        # What the shopper is looking at, so the follow-on chips can skip it.
        self.category: dict | None = None
        # Offered when the category asked for does not exist; drawn as tiles.
        self.categories: dict | None = None
        self.outfit: dict | None = None
        self.orders: dict | None = None
        self.choices: dict | None = None
        self.size: dict | None = None

    def take(self, tool_name: str, output: str | None) -> tuple[str, dict] | None:
        """Record a tool result. Returns (event_name, payload) when it had cards."""
        if output and '"action"' in output:
            try:
                action = json.loads(output).get("action")
            except (TypeError, ValueError, AttributeError):
                action = None
            if isinstance(action, dict) and action.get("type"):
                self.actions.append(action)
        if tool_name == "list_categories" and output:
            try:
                listed = [c for c in (json.loads(output).get("categories") or []) if c.get("name")]
            except (TypeError, ValueError, AttributeError):
                listed = []
            if listed:
                self.categories = {"categories": listed[:MAX_CATEGORY_TILES]}
            return None
        if tool_name == "browse_category" and output:
            try:
                found = json.loads(output)
            except (TypeError, ValueError):
                found = {}
            self.category = found.get("category") if found.get("found") else None
            if not found.get("found"):
                offered = [c for c in (found.get("categories") or []) if c.get("name")]
                if offered:
                    self.categories = {"categories": offered[:MAX_CATEGORY_TILES]}
        cards = cards_from(tool_name, output)
        if cards is None:
            return None
        name = CARD_TOOLS[tool_name]
        if name == "products":
            # Set per result, so a later ordinary search still gets reconciled.
            self.products_whole = tool_name in WHOLE_RESULT_TOOLS
            self.products_fixed = tool_name in FIXED_RESULT_TOOLS
            self.products_tool = tool_name
            self.product_results.append((tool_name, cards))
        setattr(self, name, cards)
        return name, cards

    def _pick_products(self, reply: str) -> None:
        """Of several product results, the one the reply is about.

        The agent can run two lookups at once - suggest_pieces and browse_in_size -
        and they finish in any order. Taking whichever finished last put an 18M
        shirt under a reply listing six pieces in 3M. The result whose products
        the reply actually names is the answer; a tie goes to the later one."""
        if len(self.product_results) < 2:
            return
        text = _without_choices(reply)
        best = max(enumerate(self.product_results),
                   key=lambda r: (len(keep_mentioned(r[1][1].get("items") or [], text)), r[0]))
        tool_name, cards = best[1]
        self.products = cards
        self.products_tool = tool_name
        self.products_whole = tool_name in WHOLE_RESULT_TOOLS
        self.products_fixed = tool_name in FIXED_RESULT_TOOLS

    def _use_declared(self, declared: list[str] | str | None) -> bool:
        """Draw exactly the products the agent named in its [show: ...] line.
        False when it gave no line, or named nothing it had looked up."""
        if declared is None:
            return False
        if declared == "all":
            if self.products is not None:
                self.products_fixed = True
            return True
        if not declared:
            self.products = None
            return True
        pool: dict[str, dict] = {}
        currency = None
        for _, result in self.product_results:
            currency = currency or result.get("currency")
            for item in result.get("items") or []:
                pool.setdefault(str(item.get("product_id")), item)
        picked = [pool[i] for i in dict.fromkeys(declared) if i in pool]
        if not picked:
            return False
        self.products = {"items": picked, "currency": currency}
        self.products_fixed = True
        return True

    def finalise(self, reply: str, narrowed: bool = True, narrowed_past_size: bool | None = None,
                 declared: list[str] | str | None = None) -> None:
        """Reconcile the cards with the answer the shopper actually reads.

        A tool hands back everything it found - the whole catalogue, ten search
        results - and the agent then picks a few to talk about. Sending all of
        them would show five products under a list of three. So once the reply
        exists, keep only what it mentions.

        An outfit is exempt: it *is* the answer, priced and totalled, so it is
        sent whole, and the browse that fed it is dropped as noise.
        """
        # An outfit or an order listing IS the answer, so any browse that fed it
        # is dropped as noise.
        if self.outfit is not None or self.orders is not None:
            self.products = None
        if self.orders is not None:
            listed = self.orders.get("orders") or []
            named = keep_orders_mentioned(listed, reply)
            # Nothing named is "here are your orders" - keep them all. One named
            # is "your last order was #1033", and the rest are not the answer.
            if named:
                self.orders = {**self.orders, "orders": named}
        if self.outfit is not None or self.orders is not None:
            return
        # The agent said which products to draw: that is the answer, no reading
        # of its wording needed.
        if self._use_declared(declared):
            return
        if self.products is not None:
            logger.info("No [show: ...] line from the agent; matching cards to its wording")
        self._pick_products(reply)
        # Choices are never reconciled against the wording: the whole point is
        # that they do not depend on what the agent chose to say.
        if self.products is None or self.products_fixed:
            return
        items = self.products.get("items") or []
        kept = keep_mentioned(items, _without_choices(reply))

        if self.products_whole and self.products_tool == "browse_in_size":
            # The size IS the request - "what do you have in 5Y" - so the shelf
            # is the answer even though the reply names a handful from it: the
            # shopper was told "39 pieces" and must be able to see them. Only a
            # further narrowing ("shirts in 5Y", "in blue") trims it to the names.
            past_size = narrowed if narrowed_past_size is None else narrowed_past_size
            if past_size and kept:
                self.products = _not_a_shelf(self.products, kept)
                return
            # The pieces the reply named lead, so the words and the first cards agree.
            named = {id(i) for i in kept}
            self.products = {**self.products,
                             "items": kept + [i for i in items if id(i) not in named]}
            return

        if self.products_whole:
            # "Show me dresses" is the whole shelf, however the reply sums it up
            # ("12 pieces, from the Alice to the Royal"): trimming it to the two
            # names in that sentence hid the other ten. Only a request that
            # narrowed it - a colour, an age, a size, a budget - is trimmed.
            if not narrowed:
                return
            # A bare category browse names nothing - "here is our Dress category,
            # 7 styles" - and the grid IS the answer, so it goes whole.
            #
            # But the shopper can ask for a slice of that category: "a white
            # dress for a 7 year old" still browses Dress, and the reply then
            # picks out the two that qualify. Sending the category anyway put
            # five dresses that are the wrong colour and the wrong size under an
            # answer that had already ruled them out. Once the reply names
            # products, those products are the answer.
            if kept:
                self.products = _not_a_shelf(self.products, kept)
            return
        # A reply that names nothing is an apology, a question, or a refusal - and
        # none of those should be sitting under a grid of products. Keeping the
        # whole list there was worse than showing none: it put girls' party
        # dresses under "I have nothing for a 9 year old boy".
        self.products = _not_a_shelf(self.products, kept) if kept else None

    def drop_empty_checkout(self) -> None:
        """Take out a checkout redirect when the bag is empty - unless this very turn
        put something in it. The agent said "your bag is empty" and still called
        checkout, which would have sent the shopper to an empty checkout page."""
        if any(a.get("type") == "add_to_cart" for a in self.actions):
            return
        self.actions = [a for a in self.actions
                        if not (a.get("type") == "redirect" and a.get("page") == "checkout")]

    def limit_products(self, count: int | None) -> None:
        """Hold the product row to what the shopper asked for - "2 jackets" is two
        cards, not the whole shelf. A comparison is left alone: it is exactly the
        products they named."""
        if not count or self.products is None or self.products_fixed:
            return
        items = self.products.get("items") or []
        if len(items) > count:
            self.products = _not_a_shelf(self.products, items[:count])

    def shown_products(self) -> list[dict]:
        """Every product this reply put in front of the shopper - the grid and any
        outfit - so the follow-on chips can start from where they are."""
        return [*((self.products or {}).get("items") or []),
                *((self.outfit or {}).get("items") or [])]

    def as_dict(self) -> dict:
        """Whatever was collected, for the final payload."""
        out: dict = {}
        if self.products is not None:
            items = self.products.get("items") or []
            if self.products.get("shelf"):
                out["products"] = _as_page(self.products)
            else:
                out["products"] = ({**self.products, "items": items[:MAX_CARDS]}
                                   if len(items) > MAX_CARDS else self.products)
        if self.outfit is not None:
            out["outfit"] = self.outfit
        if self.orders is not None:
            out["orders"] = self.orders
        if self.choices is not None:
            out["choices"] = self.choices
        if self.size is not None:
            out["size"] = self.size
        if self.categories is not None:
            out["categories"] = self.categories
        return out
