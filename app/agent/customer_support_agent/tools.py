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

from langchain_core.tools import tool

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.services import compare, handbook, order_changes, outfit, shopify_storefront, store_profile
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
    Returns up to 10 buyable products with price, currency and stock.
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
    """Shipping, delivery, returns, refunds and contact details, from the store handbook.

    Read the result carefully: anything still marked with an empty box has not been
    decided yet, and must NOT be guessed at - offer a human instead.
    """
    try:
        async with AsyncSessionLocal() as db:
            found = await handbook.search(db, "refund return shipping delivery policy terms", limit=4)
        return json.dumps({"handbook": found}, ensure_ascii=False)
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
            return json.dumps({"passages": await handbook.search(db, question, limit=4)}, ensure_ascii=False)
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
                         age: int = 0, budget: float = 0) -> str:
    """A few real pieces that fit what you know so far. Use on EVERY turn of an
    outfit, occasion or gift request - before you ask anything.

    Fill in only what the shopper has told you in this conversation and leave the
    rest empty (age 0, budget 0). for_who: "boy", "girl" or "baby". colour: as they
    said it, e.g. "navy". occasion: their words, e.g. "birthday party".
    Returns up to 4 in-stock pieces, one per category, already filtered to suit
    them - name each with its price. colour_matched=false means nothing came in
    that colour: say so, and that these are the nearest. still_to_ask lists what
    is missing - ask for the FIRST one only. Once age and budget are known, build
    the whole look with build_outfit, using these handles.
    """
    try:
        result = await outfit.suggest_pieces(for_who, colour, occasion, age or None, budget or None)
        return json.dumps(result, ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("suggest_pieces", exc)


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
    """
    try:
        result = await outfit.build_outfit(items, budget or None)
        return json.dumps(result, ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("build_outfit", exc)



@tool
async def browse_category(category: str) -> str:
    """Every product in ONE category the shopper named or tapped.

    category: what they actually gave you - a name like "Dress" or "Grace
      Collection", or the id a category tile sent back. Plurals are fine
      ("dresses"), and both kinds of tile - product types and collections - land
      on the right products.

    Use this whenever a shopper wants a category rather than one named product:
    "show me dresses", "what is in Winter Luxe", or a bare category name arriving
    on its own. Prefer it over search_products for a category - it returns the
    whole category, in stock, rather than a keyword guess.

    found=false means we have no such category, and it hands back the ones we do
    have: offer those instead of apologising. more_available=true means there are
    more than the ones returned.

    also_named_like_this lists products that are not in that category but carry
    the shopper's word in their own name - "Pyjama Trousers" for "pyjamas". They
    are already in products; say plainly that they sit under another heading
    rather than passing them off as part of the category.
    """
    try:
        return json.dumps(
            await shopify_storefront.category_products(category), ensure_ascii=False
        )
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
    browse_category,
    get_best_sellers,
    suggest_pieces,
    compare_products,
    add_to_cart,
    go_to_checkout,
    browse_catalogue,
    build_outfit,
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
