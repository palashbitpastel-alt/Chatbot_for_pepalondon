"""Tools for the customer support agent.

Live store data comes from fixed GraphQL operations in ``shopify_storefront`` and
``outfit``; how the store itself works - paths, collections, policies - comes from
the handbook index in ``handbook``. The agent supplies a search term, an order
number, an email or a question; it never composes a query, so a shopper cannot
steer what is asked of the Admin API. Failures come back as a plain message the
agent can relay instead of raising, so an outage degrades into an apology rather
than a broken conversation.
"""

import json
import logging
import re

from langchain_core.tools import tool

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.services import compare, extras, handbook, market, multi_buy, order_changes, outfit, shopify_storefront, size_finder, store_profile
from app.services import shopper_identity as identity
from app.services.shopify_client import ShopifyError, store_domain

logger = logging.getLogger(__name__)

UNAVAILABLE = {
    "error": "The store systems could not be reached just now.",
    "tell_customer": "I can't reach our store systems at the moment - please try again in a minute.",
}


def _fail(where: str, exc: Exception) -> str:
    logger.warning("Shopify tool %s failed: %s", where, exc)
    return json.dumps(UNAVAILABLE)


@tool
async def search_products(query: str) -> str:
    """Search the live catalogue for one thing a shopper named.

    query: a short term like "hairband" or a product name; empty lists what is sold.
    Returns up to 10 buyable products with price, currency and stock - and with
    about (a line of the store's own description), fabric, made_in, colours,
    sizes, size_range, in_collections and worn_for. details holds whatever else
    the merchant has recorded against that product - fabric, age group, sleeve
    length, care - as words, and is often where the real answer is. Use all of
    it: "is it cotton", "does it come in 12Y", "can it be machine washed",
    "would it suit a wedding" are answered from these rather than by looking the
    same piece up again. A field that is empty means the store has not said it:
    say so plainly, never fill it in yourself.
    """
    try:
        return json.dumps(await shopify_storefront.search_products(query), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("search_products", exc)


@tool
async def check_order_status(order_number: str, email: str) -> str:
    """Look up ONE order: status, items, total, tracking.

    Needs BOTH the order number ("#1027" or "1027") and the email on the order;
    it is released only when they match. found=false means they did not.
    """
    try:
        return json.dumps(
            await shopify_storefront.find_order(order_number, email), ensure_ascii=False
        )
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("check_order_status", exc)


@tool
async def add_to_cart(items: list[dict]) -> str:
    """Put products in the shopper's bag. The storefront does the adding; this
    finds the exact variant and tells it which.

    items: [{"product": "<name or handle>", "color": "Pink", "size": "5Y", "quantity": 1}]
      "this"/"it" is the product they are viewing. For variants a tool already
      gave you - build_outfit's cart_items - send [{"variant_id": "...", "quantity": 1}].
      Leave out color or size only where the product has none.
    done=true: it is going in - confirm in one line what was added.
    needs_choice: nothing was added; ask for exactly what it lists as missing,
      from its available options, then call again. Never choose a size for them.
    problems: out of stock, no such option, or not found - say which.
    """
    try:
        return json.dumps(await outfit.cart_additions(items), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("add_to_cart", exc)


_CART_NOISE = {"the", "a", "an", "my", "from", "in", "of", "and", "with", "for", "size", "colour", "color"}


def _cart_words(text: str) -> set[str]:
    words = (w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z0-9]+", text.lower()))
    return {w for w in words if w not in _CART_NOISE and len(w) > 1}


@tool
async def remove_from_cart(products: list[str] | None = None, everything: bool = False,
                           quantity: int = 0) -> str:
    """Take things out of the shopper's bag, or change how many. The storefront does it.

    products: what they named, as they said it ("the plimsolls", "pink bonnet");
      "it"/"that" with one item in the bag means that item.
    everything: true for "empty my bag", "remove everything", "clear the cart".
    quantity: 0 removes the item entirely; a number sets it to that many.
    done=true: confirm in one line what came out. not_in_bag: say which you could
    not find. which_one: more than one line matches - ask which, from the list.
    """
    cart = identity.current_cart()
    lines = [line for line in (getattr(cart, "items", None) or []) if line.variant_id]
    if not lines:
        return json.dumps({"done": False, "reason": "bag_empty",
                           "tell_customer": "Your bag is already empty."})
    if everything:
        return json.dumps({"done": True, "removed": [line.title for line in lines],
                           "action": {"type": "clear_cart"}}, ensure_ascii=False)
    asked = [p for p in (products or []) if p and p.strip()]
    if not asked and len(lines) == 1:
        asked = [lines[0].title or ""]
    if not asked:
        # "Remove from cart" with several things in it: which, or all of them?
        return json.dumps({
            "done": False,
            "which_one": [{"asked_for": "", "lines": [
                f"{l.title} ({l.variant_title})" if l.variant_title else l.title for l in lines]}],
            "tell_customer": "Ask which of these to remove, or whether to empty the whole bag.",
        }, ensure_ascii=False)
    updates: dict[str, int] = {}
    removed, not_in_bag, which_one = [], [], []
    for name in asked:
        want = _cart_words(name)
        scored = []
        for line in lines:
            have = _cart_words(f"{line.title or ''} {line.variant_title or ''}")
            hits = len(want & have)
            if hits:
                scored.append((hits, line))
        if not scored:
            if name.strip().lower() in ("it", "that", "this") and len(lines) == 1:
                scored = [(1, lines[0])]
            else:
                not_in_bag.append(name)
                continue
        best = max(h for h, _ in scored)
        top = [line for h, line in scored if h == best]
        if len(top) > 1 and len({line.title for line in top}) > 1:
            which_one.append({"asked_for": name, "lines": [f"{l.title} ({l.variant_title})" if l.variant_title else l.title for l in top]})
            continue
        for line in top:
            updates[str(line.variant_id)] = max(0, int(quantity or 0))
            removed.append(f"{line.title} ({line.variant_title})" if line.variant_title else line.title)
    result: dict = {"done": bool(updates) and not which_one, "removed" if not quantity else "changed": removed,
                    "not_in_bag": not_in_bag, "which_one": which_one}
    if updates:
        result["action"] = {"type": "update_cart", "updates": updates}
    return json.dumps(result, ensure_ascii=False)


_SIZE_LIKE = re.compile(r"^(?:\d{1,2}\s*-\s*\d{1,2}\s*[ym]|\d{1,2}\s*[ym]|\d{2}(?:\.5)?|eu\s*\d{2}|xs|s|m|l|xl|one size)$", re.I)


def _line_options(variant_title: str | None) -> tuple[str | None, str | None]:
    """(colour, size) read off a cart line's "Pink / 5Y"."""
    colour = size = None
    for part in (variant_title or "").split("/"):
        part = part.strip()
        if not part or part.lower() == "default title":
            continue
        if _SIZE_LIKE.match(part):
            size = part
        else:
            colour = part
    return colour, size


@tool
async def edit_cart_item(product: str = "", size: str = "", color: str = "", quantity: int = 0) -> str:
    """Change something already in the bag - its size, colour or how many.

    product: the bag item they mean, as they said it ("the Alice dress"); "it"
      with one item in the bag means that item. size / color: the NEW one they
      asked for, empty to keep the current one. quantity: the new count, 0 to keep.
    The storefront swaps the line itself. done=true: confirm in one line what
    changed. needs_choice / problems: that size or colour does not exist or is
    out of stock - say so and offer what it lists. which_one: ask which item.
    Removing an item is remove_from_cart, not this.
    """
    cart = identity.current_cart()
    lines = [line for line in (getattr(cart, "items", None) or []) if line.variant_id]
    if not lines:
        return json.dumps({"done": False, "reason": "bag_empty", "tell_customer": "Your bag is empty."})
    want = _cart_words(product or "")
    scored = [(len(want & _cart_words(f"{l.title or ''} {l.variant_title or ''}")), l) for l in lines]
    best = max((h for h, _ in scored), default=0)
    top = [l for h, l in scored if h == best and h > 0] or (lines if len(lines) == 1 else [])
    if len({l.title for l in top}) != 1:
        return json.dumps({"done": False, "which_one": [
            f"{l.title} ({l.variant_title})" if l.variant_title else l.title for l in (top or lines)]},
            ensure_ascii=False)
    line = top[0]
    old_colour, old_size = _line_options(line.variant_title)
    new_qty = int(quantity) if quantity and int(quantity) > 0 else int(line.quantity or 1)
    changing_variant = (size and size.strip().lower() != (old_size or "").lower()) or \
                       (color and color.strip().lower() != (old_colour or "").lower())
    if not changing_variant:
        if new_qty == line.quantity:
            return json.dumps({"done": False, "reason": "nothing_to_change",
                               "now": f"{line.title} ({line.variant_title}) x{line.quantity}"}, ensure_ascii=False)
        return json.dumps({"done": True, "changed": f"{line.title} now x{new_qty}",
                           "action": {"type": "update_cart", "updates": {str(line.variant_id): new_qty}}},
                          ensure_ascii=False)
    try:
        found = await outfit.cart_additions([{
            "product": line.handle or line.title,
            "size": (size or old_size or "").strip() or None,
            "color": (color or old_colour or "").strip() or None,
            "quantity": new_qty,
        }])
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("edit_cart_item", exc)
    if not found.get("done"):
        found.pop("action", None)
        return json.dumps({"done": False, **{k: found.get(k) for k in ("needs_choice", "problems")}},
                          ensure_ascii=False)
    new = found["lines"][0]
    if str(new["variant_id"]) == str(line.variant_id):
        return json.dumps({"done": False, "reason": "nothing_to_change"})
    return json.dumps({
        "done": True,
        "changed": f"{line.title}: {line.variant_title} -> {new.get('option')} x{new_qty}",
        "action": {"type": "swap_cart", "remove": str(line.variant_id),
                   "add": {"variant_id": new["variant_id"], "quantity": new_qty}},
    }, ensure_ascii=False)


@tool
async def product_details(product: str) -> str:
    """Everything the store says about ONE product: full description, fabric, care,
    colours, sizes, made in. Use for any question about a product itself - "is it
    machine washable?", "what is it made of?", "does it have pockets?", "is it
    lined?". product: its name as they said it ("this" = the one they are viewing).
    Answer only from what it returns; if it is not there, say the product page does
    not say and offer our team. Never guess a care instruction or a fabric.
    """
    try:
        return json.dumps(await extras.product_details(product), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("product_details", exc)


@tool
async def apply_discount_code(code: str) -> str:
    """Check a discount code and put it on their bag. code: exactly as they typed it.

    valid=true: the storefront applies it - say it is on, with its summary in a few
    words; checkout shows the saving once the bag qualifies. valid=false: say plainly
    that the code is not valid or has ended, never why beyond that.
    """
    try:
        return json.dumps(await extras.discount_code(code), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("apply_discount_code", exc)


@tool
async def delivery_estimate(country: str = "", by: str = "") -> str:
    """When an order placed now would arrive. For "will it arrive before Saturday?",
    "how long is delivery?", "can I get it by the 26th?".

    country: ISO code if they said where ("GB", "IE", "US"), else empty.
    by: the day or date they need it by, as said ("saturday", "26/09"), else empty.
    Relay arrives_between per method and, with a target, by_target (yes/maybe/no) -
    never work out dates yourself. available=false: relay tell_customer. A method with
    no stated delivery time: give its name and price, never a date.
    """
    try:
        return json.dumps(await extras.delivery_estimate(country, by), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("delivery_estimate", exc)


@tool
async def save_to_wishlist(product: str) -> str:
    """Save a product to their wishlist ("Saved for her"). product: as they named it;
    "this"/"it" = the one they are viewing or were just shown. done=true: confirm in
    one line."""
    try:
        card = await extras.product_card(product)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("save_to_wishlist", exc)
    if not card.get("found"):
        return json.dumps(card, ensure_ascii=False)
    item = {k: card.get(k) for k in ("product_id", "variant_id", "title", "image", "url", "currency")}
    item["price"] = card.get("price_from")
    return json.dumps({"done": True, "saved": card["title"], "action": {"type": "save_item", "item": item}},
                      ensure_ascii=False)


@tool
async def show_saved_items() -> str:
    """Their wishlist ("Saved for her"). No arguments. The storefront draws the
    cards: one line, how many and nothing else. Empty: say so and offer to help."""
    saved = identity.current_saved()
    items = [{"product_id": s.get("product_id"), "variant_id": s.get("variant_id"), "title": s.get("title"),
              "image": s.get("image"), "url": s.get("url"), "price": s.get("price")}
             for s in saved if isinstance(s, dict) and s.get("title")]
    return json.dumps({"count": len(items), "heading": "Saved for her", "products": items}, ensure_ascii=False)


@tool
async def remove_from_wishlist(product: str) -> str:
    """Take something off their wishlist. product: as they named it."""
    want = _cart_words(product or "")
    saved = [s for s in identity.current_saved() if isinstance(s, dict) and s.get("title")]
    scored = sorted(((len(want & _cart_words(s["title"])), s) for s in saved), key=lambda x: -x[0])
    if not scored or scored[0][0] == 0:
        return json.dumps({"done": False, "not_saved": product,
                           "saved": [s["title"] for s in saved]}, ensure_ascii=False)
    item = scored[0][1]
    return json.dumps({"done": True, "removed": item["title"],
                       "action": {"type": "unsave_item", "title": item["title"],
                                  "product_id": item.get("product_id"), "url": item.get("url")}},
                      ensure_ascii=False)


@tool
async def forget_my_preferences() -> str:
    """Forget what we remembered about them (who they shop for, age, size, colour).
    For "forget my details", "start fresh", "that's not my daughter's size any more"."""
    return json.dumps({"done": True, "action": {"type": "forget_profile"}})


@tool
async def go_to_checkout(page: str = "checkout") -> str:
    """Take the shopper to checkout - "checkout", "pay", "buy now", "place my order".
    page="cart" opens their bag instead. The storefront does the navigating."""
    target = "cart" if str(page).strip().lower() == "cart" else "checkout"
    return json.dumps({
        "done": True,
        "page": target,
        "action": {
            "type": "redirect",
            "page": target,
            "url": f"/{target}",
            "absolute_url": f"https://{store_domain()}/{target}",
        },
    })


@tool
async def compare_products(products: list[str]) -> str:
    """Put two to four products side by side. Use for "compare X and Y", "X or Y -
    which is better", "what's the difference between X and Y".

    products: every product they named, as they named it, e.g.
      ["Catherine gingham dress", "Alice floral dress"].
    Returns each product with its specs and highlights, plus in_common and
    difference already worked out: one row per attribute that differs - price,
    type, who for, sizes, colours, fabric, made in - each with a summary.
    not_found lists any name that matched nothing, with did_you_mean.
    """
    try:
        return json.dumps(await compare.compare(products), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("compare_products", exc)


@tool
async def get_store_overview() -> str:
    """What this shop is: its name, what it sells and its main categories. No arguments.

    Use for questions about the range itself - "how many products do you have",
    "what do you sell", "what kind of things do you stock". It deliberately
    carries no product count.
    """
    try:
        return json.dumps(await store_profile.overview(), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("get_store_overview", exc)


@tool
async def get_store_info() -> str:
    """Store name, currency and contact email."""
    try:
        return json.dumps(await shopify_storefront.shop_info(), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("get_store_info", exc)


@tool
async def get_store_policies() -> str:
    """Shipping, delivery, returns, refunds and contact details.

    "policies" are the store's own published policies, in the merchant's words -
    quote what they say, briefly, and never contradict them. "handbook" is what
    the team has written down internally; anything still marked with an empty box
    has not been decided yet and must NOT be guessed at - offer a human instead.
    Nothing here on the question they asked: say we have not published that and
    point them at the team, rather than inventing a rule.
    """
    try:
        async with AsyncSessionLocal() as db:
            found = await handbook.search(db, "refund return shipping delivery policy terms", limit=4)
        published = []
        try:
            published = await store_profile.policies()
        except (ShopifyError, KeyError) as exc:
            logger.info("Could not read the shop's published policies: %s", exc)
        return json.dumps({"policies": published, "handbook": found}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - a retrieval failure must not break the chat
        return _fail("get_store_policies", exc)


@tool
async def search_store_handbook(question: str) -> str:
    """Look up how this store works: account pages, collections, cart and checkout paths, policies.

    Use for "where do I find...", "how do I return...", "do you have a size guide" - anything
    about the store itself rather than a product or an order. Passages come from the store's
    own handbook. Items marked with a warning sign are unconfirmed and items marked with an
    empty box are not filled in yet: never state either as fact, and never send a link you
    were not given verbatim.
    """
    try:
        async with AsyncSessionLocal() as db:
            passages = await handbook.search(db, question, limit=4)
        published = []
        if any(word in question.lower() for word in
               ("ship", "deliver", "return", "refund", "exchange", "policy", "terms", "privacy", "cancel")):
            try:
                published = await store_profile.policies()
            except (ShopifyError, KeyError) as exc:
                logger.info("Could not read the shop's published policies: %s", exc)
        return json.dumps({"passages": passages, "policies": published}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return _fail("search_store_handbook", exc)



@tool
async def get_best_sellers(limit: int = 5) -> str:
    """What is actually selling best right now, counted from real orders.

    Use for "what's popular", "best sellers", "what do people buy", "what would
    you recommend" from a shopper you know nothing else about - not for a named
    product (search_products) and not for a whole outfit (browse_catalogue).

    limit: how many to return, 1-10; 5 is a good default.
    Ranked by units sold over the last year, cancelled orders and returned items
    excluded. Each product carries units_sold and orders alongside the usual
    price and stock. found=false with reason "no_sales_yet" means nothing has
    sold yet - say so plainly and offer to show the range instead; never dress a
    guess up as a best seller.
    """
    try:
        return json.dumps(await shopify_storefront.best_sellers(limit), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("get_best_sellers", exc)


@tool
async def suggest_pieces(for_who: str = "", colour: str = "", occasion: str = "",
                         age: int = 0, budget: float = 0, category: str = "") -> str:
    """A few real pieces that fit what you know so far. Use on EVERY turn of an
    outfit, occasion or gift request - before you ask anything.

    Fill in only what the shopper has told you in this conversation and leave the
    rest empty (age 0, budget 0). for_who: "boy", "girl" or "baby". colour: as they
    said it, e.g. "navy". occasion: their words, e.g. "birthday party".
    category: the kind of piece they named - "dress", "coat", "shoes". ALWAYS
      pass it when they named one: "a dress for a wedding" must come back as
      several dresses to choose between, not one dress and three other things.
      Leave it empty for "an outfit", "something for her", a gift.
    Returns in-stock pieces, best fit first, already suited to them - name each
    with its price. worn_for says what the store's own words place a piece at.
    occasion_matched=false means nothing in stock is written for that occasion:
    say these are the nearest rather than calling them wedding pieces.
    category_note means we sell that kind but none suits this child - say exactly
    that ("our coats are girls' only at the moment") and never "we have no coats".
    has_clothing=false means the only pieces that fit are shoes or accessories:
    there is no outfit to build for this child. Say that plainly, name where the
    range stops from nothing_wearable_fits, and do NOT ask for a budget or offer
    to build a look you cannot build.
    colour_matched=false means the same for colour. still_to_ask lists what is
    missing - ask for the FIRST one only. Once age and budget are known, build
    the whole look with build_outfit, using these handles.
    """
    try:
        result = await outfit.suggest_pieces(for_who, colour, occasion, age or None, budget or None,
                                             category=category, limit=6 if category else 4)
        return json.dumps(result, ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("suggest_pieces", exc)


@tool
async def complete_the_look(product: str, size: str = "", budget: float = 0) -> str:
    """The coordinated outfit around ONE piece - what goes with it.

    reason="no_clothing_fits": nothing wearable comes in this child's size - only
    shoes or accessories do. Say that plainly, name where the range stops, and
    offer the nearest size. Never present shoes and a belt as an outfit.

    reason="need_age": the piece is sold across several ages and nobody has said
    which. Ask how old they are, in one short question, and nothing else - then
    call this again with their answer as size. Never pick an age yourself: the
    whole look is sized from it.

    For "what goes with this", "complete the look", "style this dress", or a
    shopper looking at a piece who wants the whole outfit. product: the piece
    they named or are viewing. size / budget: only if they said one.
    Returns the look already priced, with its total and the exact variants; the
    storefront draws it with a tick per piece and an add-the-look button. Say in
    one line what you have put together and the total, nothing more.
    """
    try:
        return json.dumps(await outfit.complete_the_look(product, size or None, budget or None),
                          ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("complete_the_look", exc)


@tool
async def browse_catalogue() -> str:
    """Everything buyable right now, by category - use before build_outfit.

    For an outfit, gift or occasion rather than one named product. Returns each
    product's handle, category, price, colours, sizes, and the store currency.
    """
    try:
        return json.dumps(await outfit.browse_catalogue(), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("browse_catalogue", exc)


@tool
async def build_outfit(items: str | list, budget: float = 0) -> str:
    """Price a look exactly and get its variant ids. Never add prices up yourself.

    items: JSON array using handles/colours/sizes from browse_catalogue, e.g.
      [{"handle": "gingham-dress", "color": "Pink", "size": "5Y", "quantity": 1}]
    Omit color/size where the product has none. budget: 0 if not given.
    Returns total, within_budget, cart_items (variant ids for the storefront), and
    problems listing the colours/sizes that do exist so you can swap and retry.
    
    An outfit is one of each kind of thing: a top, a bottom, shoes, a coat.
    Never send two of the same kind - two shirts is not a look. Nor a piece for
    the other child, nor one sized for another age, nor nightwear. left_out
    names anything dropped for those reasons, with which: mention it in half a
    sentence where it changes the answer, and never present it as in the look.
    not_an_outfit comes back when what you sent holds nothing to wear: say so
    rather than calling shoes and a belt a look.
    """
    try:
        result = await outfit.build_outfit(items, budget or None)
        if result.get("outfit"):
            pieces = sum(int(i.get("quantity") or 1) for i in result["outfit"])
            result["multi_buy"] = multi_buy.summary(await multi_buy.tiers(), pieces,
                                                      result.get("total"), result.get("currency"))
        return json.dumps(result, ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("build_outfit", exc)


@tool
async def find_size(product: str = "", age: float = 0, height_cm: float = 0,
                    chest_cm: float = 0, usual_size: str = "") -> str:
    """Recommend ONE size for a child, from what the shopper told you.

    Use for "what size", "will it fit", a height or measurement, or "she usually
    wears 5-6Y". product: the piece they are viewing or named ("this" = the one
    they are viewing); empty for a general answer. Fill only what they gave you
    (0 / "" otherwise): age in years, height_cm, chest_cm, usual_size like "5-6Y".
    Returns recommended (e.g. "6-7Y"), fit_note (why), alternatives. The
    storefront draws the size card itself: say the size and the fit note in one
    line. found=false: ask for their age and height, in one question.
    """
    try:
        result = await size_finder.for_product(
            product or None, age=age or None, height_cm=height_cm or None,
            chest_cm=chest_cm or None, usual_size=usual_size or None,
        )
        return json.dumps(result, ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("find_size", exc)



@tool
async def list_categories() -> str:
    """Every category we sell, by name, with how many pieces in each. No arguments.

    Use for "what categories do you have", "list your categories", "what kinds of
    things do you sell". The storefront draws each as a tile, so name them all in
    one short sentence and stop - no descriptions, no counts, no list of products.
    """
    try:
        found = await shopify_storefront.categories()
        return json.dumps({
            "count": found["count"],
            "categories": [
                {k: c.get(k) for k in ("id", "name", "image", "image_alt", "url", "product_count")}
                for c in found["categories"]
            ],
        }, ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("list_categories", exc)


@tool
async def browse_in_size(size: str) -> str:
    """Every piece that comes in ONE size and is in stock in it.

    size: as they said it - "12Y", "18M", "5-6Y". Use whenever a size is the
    whole request ("what do you have in 12Y", "show me pieces in 2Y"), and never
    search_products for it: a size is not a word in a product's name, so a search
    finds only the few that spell it out. in_this_size on each piece is the exact
    label it is sold under (a 12Y request matches an 11-12Y piece). found=false:
    say plainly that nothing comes in that size and offer the nearest.
    Name the pieces with their prices - a count on its own ("8 pieces come in
    12Y") leaves the shopper reading a number with unnamed cards beside it.
    """
    try:
        return json.dumps(await shopify_storefront.products_in_size(size), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("browse_in_size", exc)



def _for_this_shopper(found: dict) -> dict:
    """Drop the pieces meant for a different child.

    A shopper who has said "my daughter" and then opens Trousers, Shorts &
    Skirts was being shown boys' chinos: the collection is mixed, and nothing
    downstream knew who we were shopping for. Pieces the store has not tagged
    for anyone stay - they suit either - and the reply says what was left out,
    so "show me the boys' ones too" still works.
    """
    who = identity.shopping_for()
    products = found.get("products") if isinstance(found, dict) else None
    if not who or not isinstance(products, list):
        return found
    other = {"Girls": "Boys", "Boys": "Girls"}.get(who)
    if not other:
        return found

    def theirs(product: dict) -> bool:
        tagged = product.get("for") or product.get("audience") or []
        if isinstance(tagged, str):
            tagged = [tagged]
        if tagged:
            return who in tagged or not (set(tagged) & {"Girls", "Boys"})
        # Untagged: the name still gives it away often enough to matter.
        return other.rstrip("s").lower() not in (product.get("title") or "").lower()

    kept = [p for p in products if theirs(p)]
    if len(kept) == len(products):
        return found
    found = dict(found)
    found["products"] = kept
    found["count"] = len(kept)
    found["filtered_to"] = who
    found["also_here_for_the_other"] = len(products) - len(kept)
    return found


def _in_their_colour(found: dict) -> dict:
    """Put the colour they asked for first, and say when we have none of it.

    Told "blue", the shopper was still shown camel, burgundy and navy: the
    colour lived in the Understood panel and nowhere a listing could read it.
    A colour nothing comes in is not a reason to show nothing - it is a reason
    to say so.
    """
    wanted = identity.wants_colour()
    products = found.get("products") if isinstance(found, dict) else None
    if not wanted or not isinstance(products, list) or not products:
        return found

    def comes_in(product: dict) -> bool:
        if wanted in (product.get("title") or "").lower():
            return True
        return any(wanted in (v.get("option") or "").lower()
                   for v in (product.get("variants") or []) if v.get("available"))

    theirs = [p for p in products if comes_in(p)]
    found = dict(found)
    found["asked_for_colour"] = wanted
    if not theirs:
        found["colour_matched"] = False
        return found
    found["colour_matched"] = True
    found["products"] = theirs
    found["count"] = len(theirs)
    found["also_here_in_other_colours"] = len(products) - len(theirs)
    return found

@tool
async def browse_category(category: str) -> str:
    """Every product in ONE category the shopper named or tapped.

    category: what they actually gave you - a name like "Dress" or "Grace
      Collection", or the id a category tile sent back. Plurals are fine
      ("dresses"), and both kinds of tile - product types and collections - land
      on the right products.

    Use this whenever a shopper wants a category rather than one named product:
    "show me dresses", "what is in Winter Luxe", or a bare category name arriving
    on its own. Who it is for counts too: "girls", "for boys", "baby" return the
    pieces tagged for them. Prefer it over search_products for a category - it returns the
    whole category, in stock, rather than a keyword guess.

    found=false means we have no such category, and it hands back the ones we do
    have: offer those instead of apologising. more_available=true means there are
    more than the ones returned.

    colour_matched=false means they asked for a colour and nothing here comes in
    it: say that plainly before showing what there is. Where it is true, the
    other colours were left out and also_here_in_other_colours counts them.

    filtered_to means the shopper has told you who they are shopping for and the
    pieces for the other child were left out; also_here_for_the_other says how
    many. Mention it in a half-sentence ("the boys' pieces are there too if you
    want them") and never present them as if the whole shelf were shown.

    also_named_like_this lists products that are not in that category but carry
    the shopper's word in their own name - "Pyjama Trousers" for "pyjamas". They
    are already in products; say plainly that they sit under another heading
    rather than passing them off as part of the category.
    """
    try:
        found = await shopify_storefront.category_products(category)
        return json.dumps(_in_their_colour(_for_this_shopper(found)), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("browse_category", exc)


# Two different reasons the shopper's own history is unavailable, and they need
# different answers. When the deployment trusts the storefront's sign-in, nobody
# to act for really does mean nobody is signed in - so say so and point at the
# account. When it does not, the shopper may well be signed in and we simply
# cannot use it; telling them to log in would then be wrong, and worse, they
# would do it and nothing would change.

NOT_SIGNED_IN_ASK_LOGIN = {
    "signed_in": False,
    "reason": "not_logged_in",
    "tell_customer": (
        "You will need to be signed in for me to see your orders. Log in - or create an "
        "account if you do not have one yet - and I can pick up right here. If you would "
        "rather not, give me an order number and the email it was placed with and I can "
        "look that one up for you."
    ),
}

NOT_SIGNED_IN_ASK_ORDER = {
    "signed_in": False,
    "reason": "identity_not_trusted",
    "tell_customer": (
        "I can only pull up your order history once I know it is you. Give me an order "
        "number and the email it was placed with and I can check that order directly."
    ),
}


def _not_signed_in() -> str:
    if settings.TRUST_STOREFRONT_CUSTOMER:
        return json.dumps(NOT_SIGNED_IN_ASK_LOGIN)
    return json.dumps(NOT_SIGNED_IN_ASK_ORDER)


@tool
async def get_my_order_history() -> str:
    """Past orders for the shopper this chat belongs to. Takes no arguments.

    Only works when the storefront has signed them in and this deployment trusts
    that. signed_in=false comes with a reason: "not_logged_in" means they are not
    signed in, so relay tell_customer and ask them to log in or sign up;
    "identity_not_trusted" means ask for an order number and email instead.
    Either way relay tell_customer. You cannot look up anybody else with this.
    """
    shopper = identity.current()
    if shopper is None:
        return _not_signed_in()
    try:
        return json.dumps(await shopify_storefront.customer_orders(shopper.email), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("get_my_order_history", exc)


@tool
async def recommend_for_me() -> str:
    """Suggest products for this shopper based on what they have bought before. No arguments.

    Use when a signed-in shopper asks what they might like, or for a gift for the
    same child. Returns products they do not already own. interests lists what
    they buy most, most often first. Each pick carries because (tied to a real
    past purchase, named in like_purchase) and about (the product's own first
    line) - the only material for saying why; never add a feature beyond them.
    signed_in=false carries the same reason codes as get_my_order_history:
    relay its tell_customer rather than writing your own.
    """
    shopper = identity.current()
    if shopper is None:
        return _not_signed_in()
    try:
        history = await shopify_storefront.customer_orders(shopper.email, limit=10)
        return json.dumps(await outfit.recommend_from_orders(history["orders"]), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("recommend_for_me", exc)


# ── Cancelling and re-addressing ───────────────────────────────────────────
# Two steps on purpose. The first only *asks*; nothing is written until the
# second, and the second cannot be reached without the token the first returns.


@tool
async def request_order_change(order_number: str, email: str, action: str) -> str:
    """Step ONE of cancelling an order or moving its delivery address. Writes nothing.

    action: "cancel" or "change_address".
    Needs BOTH the order number and the email on the order, exactly like
    check_order_status; found=false means they did not match.

    On success returns what the order contains, the reasons to offer the shopper,
    and ask_shopper_for - one detail on the order they must confirm before
    anything happens. Ask for that, the reason, and (for an
    address) the new address, then call confirm_order_change ONCE with all of it.
    eligible=false means the change is not possible - relay tell_customer and stop.
    """
    try:
        result = await order_changes.begin(
            order_number, email, action, session_id=identity.current_session()
        )
        return json.dumps(result, ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("request_order_change", exc)


@tool
async def confirm_order_change(
    verification_answer: str = "",
    reason_code: str = "",
    reason_text: str = "",
    new_address: str = "",
) -> str:
    """Step TWO: actually cancel the order or move it. This is irreversible.

    Only call once the shopper has said yes to the exact change, in words.
    It finds the order itself, from the one request_order_change started in this
    conversation - there is no token to keep hold of.

    verification_answer: what they gave for ask_shopper_for.
    reason_code: one of the "code" values that request_order_change returned.
    reason_text: their own words - required when reason_code is "other", welcome
      otherwise. Never invent it.
    new_address: address changes only. A JSON object; send only the parts that
      change, the rest is kept.
      {"address1": "...", "address2": "...", "city": "...", "zip": "...",
       "first_name": "...", "last_name": "...", "phone": "...",
       "province_code": "...", "country_code": "GB"}

    done=false with verification_failed means the answer was wrong - say so and
    let them try again. Relay tell_customer either way.
    """
    parsed: dict = {}
    if new_address:
        if isinstance(new_address, dict):
            parsed = new_address
        else:
            try:
                parsed = json.loads(new_address)
            except (TypeError, ValueError):
                return json.dumps(
                    {
                        "done": False,
                        "reason": "bad_address",
                        "tell_customer": "Could you give me the new address again?",
                    }
                )
        if not isinstance(parsed, dict):
            parsed = {}

    try:
        result = await order_changes.commit(
            verification_answer=verification_answer,
            reason_code=reason_code,
            reason_text=reason_text,
            new_address=parsed,
            session_id=identity.current_session(),
        )
        return json.dumps(result, ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("confirm_order_change", exc)


CUSTOMER_SUPPORT_TOOLS = [
    search_products,
    browse_in_size,
    list_categories,
    browse_category,
    get_best_sellers,
    suggest_pieces,
    compare_products,
    add_to_cart,
    remove_from_cart,
    edit_cart_item,
    apply_discount_code,
    go_to_checkout,
    product_details,
    delivery_estimate,
    save_to_wishlist,
    show_saved_items,
    remove_from_wishlist,
    forget_my_preferences,
    browse_catalogue,
    build_outfit,
    complete_the_look,
    find_size,
    check_order_status,
    request_order_change,
    confirm_order_change,
    get_my_order_history,
    recommend_for_me,
    get_store_overview,
    get_store_info,
    get_store_policies,
    search_store_handbook,
]


# ── Market prices ───────────────────────────────────────────────────────────
# Every tool that quotes a price answers in the shopper's own market (rupees in
# India, pounds in the UK), straight from Shopify's price lists. Orders are left
# alone: they keep the currency they were paid in.
MARKET_PRICED = {
    "search_products", "browse_in_size", "browse_category", "get_best_sellers", "browse_catalogue",
    "suggest_pieces", "build_outfit", "complete_the_look", "compare_products", "recommend_for_me",
    "product_details", "add_to_cart", "find_size",
}


def _priced(run):
    async def wrapped(*args, **kwargs):
        out = await run(*args, **kwargs)
        try:
            data = json.loads(out)
        except (TypeError, ValueError):
            return out
        return json.dumps(await market.localize(data), ensure_ascii=False)
    return wrapped


for _t in CUSTOMER_SUPPORT_TOOLS:
    if _t.name in MARKET_PRICED and getattr(_t, "coroutine", None):
        _t.coroutine = _priced(_t.coroutine)
