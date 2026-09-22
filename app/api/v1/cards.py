"""Product cards for the chat clients.

A tool result is JSON meant for the model; a storefront needs a picture, a price
and somewhere to click. This turns one into the other, and is shared by every
chat endpoint so a client gets the same cards whether it streams the reply or
takes it in one piece.
"""

import json
import re

# Tools whose result a client can render as cards, and the key it arrives under.
CARD_TOOLS = {
    "search_products": "products",
    "browse_category": "products",
    "get_best_sellers": "products",
    "browse_catalogue": "products",
    "suggest_pieces": "products",
    "compare_products": "products",
    "recommend_for_me": "products",
    "build_outfit": "outfit",
    "get_my_order_history": "orders",
    "check_order_status": "orders",
    # Not a card - a set of buttons. Same idea though: the shopper should be
    # tapping a choice, not reading the agent recite seven of them.
    "request_order_change": "choices",
}
MAX_CARDS = 12

# Tools whose product list IS the answer, not a shortlist the agent then talks
# about. A category browse is the shopper's own request drawn back at them, so
# it is sent whole - trimming it to the few products the reply names would empty
# a grid the shopper explicitly asked to see.
WHOLE_RESULT_TOOLS = {"browse_category"}

# Tools whose products are never trimmed to the wording. A comparison is every
# product in it, whichever of them the reply happens to name in full.
FIXED_RESULT_TOOLS = {"compare_products"}


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


def _stem(word: str) -> str:
    """Fold simple plurals so "Mary Janes" still finds "Mary Jane"."""
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


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
    said = _words(reply)
    title_words = [(item, _words(item.get("title") or "")) for item in items]

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
            if distinctive & said:
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


def _card(item: dict) -> dict:
    """The fields a storefront needs to draw a product and link to it."""
    return {
        "product_id": item.get("product_id"),
        "variant_id": item.get("variant_id"),
        "title": item.get("title"),
        "option": item.get("option"),
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

    if tool_name == "build_outfit":
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
        }

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
    return result


class CardCollector:
    """Gathers cards across a turn so they can be sent mid-stream and at the end.

    A turn may call several tools; the last product result is the one the reply
    is actually about, so later cards replace earlier ones of the same kind.
    """

    def __init__(self) -> None:
        self.products: dict | None = None
        self.products_whole = False
        self.products_fixed = False
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

    def take(self, tool_name: str, output: str | None) -> tuple[str, dict] | None:
        """Record a tool result. Returns (event_name, payload) when it had cards."""
        if output and '"action"' in output:
            try:
                action = json.loads(output).get("action")
            except (TypeError, ValueError, AttributeError):
                action = None
            if isinstance(action, dict) and action.get("type"):
                self.actions.append(action)
        if tool_name == "browse_category" and output:
            try:
                found = json.loads(output)
            except (TypeError, ValueError):
                found = {}
            self.category = found.get("category") if found.get("found") else None
            if not found.get("found"):
                offered = [c for c in (found.get("categories") or []) if c.get("name")]
                if offered:
                    self.categories = {"categories": offered[:MAX_CARDS]}
        cards = cards_from(tool_name, output)
        if cards is None:
            return None
        name = CARD_TOOLS[tool_name]
        if name == "products":
            # Set per result, so a later ordinary search still gets reconciled.
            self.products_whole = tool_name in WHOLE_RESULT_TOOLS
            self.products_fixed = tool_name in FIXED_RESULT_TOOLS
        setattr(self, name, cards)
        return name, cards

    def finalise(self, reply: str) -> None:
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
        # Choices are never reconciled against the wording: the whole point is
        # that they do not depend on what the agent chose to say.
        if self.products is None or self.products_fixed:
            return
        items = self.products.get("items") or []
        kept = keep_mentioned(items, _without_choices(reply))

        if self.products_whole:
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
                self.products = {**self.products, "items": kept}
            return
        # A reply that names nothing is an apology, a question, or a refusal - and
        # none of those should be sitting under a grid of products. Keeping the
        # whole list there was worse than showing none: it put girls' party
        # dresses under "I have nothing for a 9 year old boy".
        self.products = {**self.products, "items": kept} if kept else None

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
            self.products = {**self.products, "items": items[:count]}

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
            out["products"] = ({**self.products, "items": items[:MAX_CARDS]}
                               if len(items) > MAX_CARDS else self.products)
        if self.outfit is not None:
            out["outfit"] = self.outfit
        if self.orders is not None:
            out["orders"] = self.orders
        if self.choices is not None:
            out["choices"] = self.choices
        if self.categories is not None:
            out["categories"] = self.categories
        return out
