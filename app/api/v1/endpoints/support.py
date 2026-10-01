"""Customer-facing support chat.

Streams the customer support agent's reply over Server-Sent Events so the
shopper sees words appear as the agent writes them, plus a marker whenever the
agent looks something up. The agent is pinned here — unlike ``/chat`` this
endpoint takes no agent name, so a public client can never point it at the
admin agent and read internal business data.
"""

import json
import logging
import re
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent.customer_support_agent import CUSTOMER_SUPPORT_AGENT
from app.agent.customer_support_agent import tools
from app.core.config import settings
from app.agent.customer_support_agent.shopper_context import (
    Cart,
    Customer,
    PageContext,
    describe,
    with_context,
)
from app.api.v1.cards import CardCollector, cards_from, keep_mentioned, split_show, _card
from app.services import audience, market, multi_buy, needs, outfit, shopify_storefront, shopper_identity as identity
from app.services import size_finder, store_profile, suggestions, understanding
from app.services.shopify_client import ShopifyError
from app.db.models import ChatMessage, ShopperState
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["support"])

# Every stored turn is replayed on every step of the next turn, so this stays
# small: a shopper's thread rarely needs more than the last few exchanges.
HISTORY_LIMIT = 16
# Support conversations share the chat_messages table with the admin chat, so
# they carry their own session-id prefix and only ever load their own history.
SESSION_PREFIX = "cs_"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # stop nginx buffering the stream
}



class SizeRequest(BaseModel):
    """The Smart Size Finder quiz. Every answer is optional; more answers, better fit."""

    product: str | None = Field(default=None, max_length=200)   # handle or title
    age: float | None = Field(default=None, ge=0, le=16)
    height_cm: float | None = Field(default=None, ge=40, le=190)
    chest_cm: float | None = Field(default=None, ge=30, le=120)
    usual_size: str | None = Field(default=None, max_length=20)


class SupportChatRequest(BaseModel):
    # Empty on purpose: the widget sends "" when a shopper opens it, and gets
    # the welcome screen back rather than a trip through the agent.
    message: str = Field(default="", max_length=4000)
    session_id: str | None = None
    # Optional state from the storefront widget: what is in the cart, who is
    # signed in, and which page they are on. All of it is a claim from the
    # browser - see shopper_context and identity for what it may and may not do.
    cart: Cart | None = None
    customer: Customer | None = None
    context: PageContext | None = None
    # Kept in the shopper's own browser: their wishlist, and what they told us on
    # earlier visits (who they shop for, age, size, colour).
    saved: list[dict] = Field(default_factory=list, max_length=30)
    profile: dict[str, str] = Field(default_factory=dict)


# ── Saying hello ───────────────────────────────────────────────────────────
# The agent is told nothing about the shop it works for, so a "hi" came back as
# "Hi Subham, what can I help you find?" - no name, no idea what we sell. A bare
# hello now carries a short store briefing for that one turn; every other turn
# stays as lean as before, since this block would otherwise ride along always.

_GREETING_WORDS = {
    "hi", "hii", "hiii", "hello", "helo", "hey", "heya", "hiya", "howdy", "yo",
    "namaste", "hola", "greetings", "good", "morning", "afternoon", "evening",
    "there", "all", "team", "everyone", "folks", "sup",
}


def _is_greeting(message: str) -> bool:
    """A hello and nothing else - "hi", "hello there", "good morning"."""
    words = re.findall(r"[a-z]+", (message or "").lower())
    return 0 < len(words) <= 4 and all(w in _GREETING_WORDS for w in words)


async def _store_briefing(customer: Customer | None) -> str:
    """Who we are, for a hello. Every part is optional: a slow lookup must not
    cost the shopper their greeting."""
    # Four categories, not six: handed six the agent read every one out, like a
    # stock list.
    about = await store_profile.overview(limit=4)
    lines = ["[Store - use this to greet them]"]
    if about.get("name"):
        lines.append(f"Name: {about['name']}")
    lines.append(f"What we sell (say it in your own words): {about['what_we_sell']}")
    if about.get("categories"):
        lines.append("Our main categories: " + ", ".join(about["categories"]))
    signed_in = bool(customer and customer.logged_in)
    lines.append("Signed in: yes - welcome them back" if signed_in else "Signed in: no")
    return "\n".join(lines)


async def _names_a_kind(message: str) -> bool:
    """Did they narrow the shelf by naming a kind of piece - "just dresses"?

    "Everything in 12Y" is the whole shelf and the grid is the answer; "just
    dresses please" is not, and leaving the shelf up put seven jumpers and a
    jacket under a reply that named one dress. The kinds are the store's own
    product types, so this follows whatever it sells.
    """
    words = {store_profile._singular(w) for w in re.findall(r"[a-z]+", message.lower()) if len(w) > 2}
    if not words:
        return False
    try:
        catalogue = await outfit.browse_catalogue()
    except Exception:  # noqa: BLE001 - a card decision must not cost the reply
        logger.debug("No catalogue to check the message against", exc_info=True)
        return False
    kinds = {store_profile._singular(str(p["category"]))
             for p in catalogue["products"] if p.get("category")}
    return bool(kinds & words)


_BUDGET_FIELD = re.compile(r"^(Under|Around)\s*(\D{0,4}?)\s*([\d,.]+)$")


def _budget_amount(understood: dict, showing: str | None) -> float | None:
    """The budget as a number in the shop's money, or None if it is in another."""
    field = next((f for f in understood.get("fields") or [] if f["key"] == "budget"), None)
    m = _BUDGET_FIELD.match(str((field or {}).get("value") or "").strip())
    if not m:
        return None
    sign = (m.group(2) or "").strip()
    if sign and sign != (needs.symbol(showing) or "").strip():
        return None
    try:
        return float(m.group(3).replace(",", ""))
    except ValueError:
        return None


async def _budget_in_their_money(understood: dict, showing: str | None) -> None:
    """Rewrite the Understood panel's budget into the money this shop is quoting.

    A shopper who typed "around £400" was shown "Around £400" beside rupee
    prices for the rest of the conversation. Keeping their sign was deliberate -
    turning £400 into ₹400 once made a budget a hundredth of what they meant -
    but now the shop can say what £400 is worth here, so the panel says it too.
    """
    field = next((f for f in understood.get("fields") or [] if f["key"] == "budget"), None)
    ours = needs.symbol(showing)
    if not field or not ours:
        return
    m = _BUDGET_FIELD.match(str(field.get("value") or "").strip())
    if not m or not m.group(2) or m.group(2).strip() == ours.strip():
        return
    try:
        catalogue = await outfit.browse_catalogue()
        ids = [p["product_id"] for p in (catalogue.get("products") or [])
               if p.get("product_id") and p.get("price_from")][:9]
        converted = await market.in_our_money(
            float(m.group(3).replace(",", "")), m.group(2).strip(), ids)
    except (ShopifyError, KeyError, ValueError):
        return
    if converted:
        field["value"] = f"{m.group(1)} {ours}{converted['our_budget']:,.0f}"
        # What they typed, kept for the agent's own reading.
        field["as_they_said_it"] = m.group(0)


# Said to the agent when it answered with products it never looked up. Worded as
# how this shop works, not as a rebuke: that is the framing the model follows.
_LOOK_IT_UP = ("[How this shop works: every product you name must come from a tool you call this "
               "turn - the storefront can only draw a picture for what a tool returned, and a name "
               "from earlier in the chat may not exist. Your last draft named products without "
               "looking them up. Call the right tool for what they are asking now, with everything "
               "they have told you, and answer only from what it returns.]")


async def _named_without_looking(reply: str, cart: Cart | None, on_screen: list[str] | None = None) -> bool:
    """Does a reply written with no tool at all put products in front of the shopper?

    "They are under 1" came back as six baby pieces from memory - three of which
    the shop does not sell - and "please show me those products" copied the same
    list again. Nothing was looked up, so there were no cards, and the prompt's
    "call the tool every time" had already lost to the list sitting in the
    transcript. This only notices that it happened; what to show is still the
    agent's call, made again with a tool.

    Two signs, either enough: a real product of ours is named (one in their bag
    is fine - cart questions are answered from the context block), or two or more
    prices are quoted, which is a list whether or not its names are real.
    """
    if not reply.strip():
        return False
    in_bag = {(line.title or "").lower() for line in (cart.items if cart else [])}
    # Cards on their screen were looked up already: answering "which is cheaper,
    # the first or the second?" from them is not a reply from memory.
    in_bag |= {str(t).lower() for t in (on_screen or [])}
    try:
        catalogue = await outfit.browse_catalogue()
    except Exception:  # noqa: BLE001 - a check must never cost the reply
        logger.debug("No catalogue to check the reply against", exc_info=True)
        catalogue = {}
    ours = [p for p in (catalogue.get("products") or [])
            if p.get("title") and p["title"].lower() not in in_bag]
    if keep_mentioned(ours, reply):
        return True
    if (cart and cart.items) or on_screen:
        return False
    # The shopper's own currency too: a made-up "1,299 INR" list must be caught.
    codes = {c for c in (catalogue.get("currency"), cart.currency if cart else None, market._showing.get()) if c}
    if not codes:
        return False
    money = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:" + "|".join(map(re.escape, codes)) + r")\b", re.I)
    return len(money.findall(reply)) >= 2


def _welcome_handles() -> list[str]:
    """The collections the merchant pinned to the welcome screen, in their order."""
    return [h.strip() for h in settings.SUPPORT_WELCOME_COLLECTIONS.split(",") if h.strip()]


async def _welcome_text() -> str:
    """The greeting. Falls back to the store's own name so it is never generic."""
    configured = settings.SUPPORT_WELCOME_MESSAGE.strip()
    if configured:
        return configured
    try:
        name = await store_profile.store_name()
    except Exception:  # noqa: BLE001 - a greeting must not fail on a slow shop lookup
        logger.warning("Could not read the shop name for the greeting", exc_info=True)
        name = "our store"
    return f"Welcome to {name} 👋\nAsk me anything you are interested in."


async def _welcome_back(shopper: identity.Shopper) -> dict | None:
    """"Welcome back, Charlotte": what they bought before and what goes with it.

    Only ever called for a shopper the request has proved (a signed block), since
    it reads their order history. Empty history still greets them by name.
    """
    history = await shopify_storefront.customer_orders(shopper.email, limit=10)
    orders = history.get("orders") or []
    bought: list[dict] = []
    seen: set = set()
    for order in orders:
        for line in order.get("items") or []:
            key = line.get("product_id") or line.get("title")
            if key and key not in seen:
                seen.add(key)
                bought.append(_card(line) | {"placed_on": order.get("placed_on")})
    picks = None
    picks_because = None
    if orders:
        found = await outfit.recommend_from_orders(orders)
        picks = cards_from("recommend_for_me", json.dumps(found, ensure_ascii=False))
        picks_because = "bought_before"
    else:
        # Signed in but nothing bought yet: still worth showing them something,
        # and what the shop sells most is the safest thing to show.
        try:
            found = await shopify_storefront.best_sellers(limit=3)
            picks_because = "popular"
            if not (found.get("products") or []):
                # A shop with no orders yet has no best sellers; show what it
                # has rather than an empty panel.
                found = await shopify_storefront.search_products("", limit=3)
                picks_because = "new_in"
            picks = cards_from("get_best_sellers", json.dumps(found, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - a greeting must not fail on this
            logger.warning("Could not load picks for a new customer", exc_info=True)
    # "Goes with it": what completes the last thing they bought.
    goes_with = []
    anchor = next((b for b in bought if b.get("title")), None)
    if anchor:
        try:
            look = await outfit.complete_the_look(anchor.get("handle") or anchor["title"], pieces=2)
            goes_with = [_card(i) for i in (look.get("outfit") or [])
                         if i.get("title") != anchor["title"]][:2]
        except Exception:  # noqa: BLE001 - a nicety, never a blocker on the greeting
            logger.warning("Could not build goes-with for the welcome panel", exc_info=True)
    return {
        "first_name": shopper.first_name,
        "previously_bought": bought[:4],
        "goes_with": goes_with,
        "goes_with_for": anchor["title"] if anchor and goes_with else None,
        "picks": picks,
        "picks_because": picks_because,
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _resolve_session(session_id: str | None) -> str:
    """Reuse a support session, or start one. Ids from elsewhere are not accepted."""
    if session_id and session_id.startswith(SESSION_PREFIX):
        return session_id
    return SESSION_PREFIX + uuid.uuid4().hex


async def _user_messages(session_id: str, limit: int = 24) -> list[str]:
    """What the shopper has said in this chat, oldest first - more of it than the
    model replays, so what they told us early on is still read."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatMessage.content)
                .where(ChatMessage.session_id == session_id, ChatMessage.role == "user")
                .order_by(ChatMessage.id.desc())
                .limit(limit)
            )
        ).scalars().all()
    return list(reversed(rows))


async def _load_history(session_id: str, asking: str = "") -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id.desc())
                .limit(HISTORY_LIMIT)
            )
        ).scalars().all()
    return _without_repeats([(m.role, _without_cards_note(m.content)) for m in reversed(rows)], asking)


def _without_repeats(turns: list[tuple[str, str]], asking: str) -> list[tuple[str, str]]:
    """Drop earlier turns that asked exactly what is being asked now.

    A shopper who asks the same thing twice must get it looked up twice: the
    storefront draws its cards from the tool result, so a reply copied out of the
    transcript arrives with no products beside it. Left in, those turns are also
    the evidence the model reasons from - four identical exchanges read as "this
    is already answered", and by then no wording in the system prompt reliably
    wins. Removing them takes the copy away, and the question arrives fresh.

    Only exact repeats go; everything else stays, so "this" and "it" still refer
    to what the shopper was looking at.
    """
    key = " ".join(asking.lower().split())
    # Short answers ("yes", "show me") repeat all the time; dropping the earlier
    # one took the assistant's offer it answered with it.
    if not key or len(key.split()) < 3:
        return turns

    kept: list[tuple[str, str]] = []
    i = 0
    while i < len(turns):
        role, content = turns[i]
        if role == "user" and " ".join(content.lower().split()) == key:
            i += 1
            while i < len(turns) and turns[i][0] != "user":
                i += 1          # and the answer it drew, which is what gets copied
            continue
        kept.append((role, content))
        i += 1
    return kept


_CARDS_NOTE = "[Cards shown under this reply, in order:"


def _without_cards_note(text: str) -> str:
    """A saved reply as the shopper saw it: without the agent's note of its cards."""
    at = (text or "").find(_CARDS_NOTE)
    return text if at == -1 else text[:at].rstrip()


async def _save_turn(session_id: str, message: str, reply: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                ChatMessage(session_id=session_id, role="user", content=message),
                ChatMessage(session_id=session_id, role="assistant", content=reply),
            ]
        )
        await db.commit()


class ShelfRequest(BaseModel):
    """The rest of a shelf the chat opened: sent back exactly as the products
    event carried it, plus the cards the shopper already has."""

    tool: str = Field(max_length=40)
    arg: str = Field(max_length=200)
    for_: str | None = Field(default=None, alias="for", max_length=20)
    colour: str | None = Field(default=None, max_length=40)
    shown: list[str] = Field(default_factory=list, max_length=300)
    # How many more to send; 0 is everything that is left.
    limit: int = Field(default=8, ge=0, le=100)
    country: str | None = Field(default=None, max_length=8)
    currency: str | None = Field(default=None, max_length=8)


@router.post("/support/more")
async def support_more(req: ShelfRequest) -> dict:
    """The next cards of a shelf - "Show 8 more", or "Show all" with limit 0.

    The first reply carries only the first page, so a 39-piece answer is not 39
    cards loaded at once; this fetches the shelf again with the same audience and
    colour and returns what the shopper has not seen yet, in the same order.
    """
    if req.tool not in tools.SHELF_TOOLS:
        raise HTTPException(status_code=400, detail="Not a shelf")
    audience = identity.set_audience(req.for_)
    colour = identity.set_colour(req.colour)
    country = market.set_country(req.country)
    showing = market.set_showing(req.currency)
    try:
        await audience.ensure()
        found = await market.localize(await tools.shelf(req.tool, req.arg))
    except ShopifyError:
        logger.warning("Could not fetch more of shelf %s %s", req.tool, req.arg, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not load more products") from None
    finally:
        identity.reset_audience(audience)
        identity.reset_colour(colour)
        market.reset_country(country)
        market.reset_showing(showing)
    page = cards_from(req.tool, json.dumps(found, ensure_ascii=False)) or {"items": []}
    seen = set(req.shown)
    left = [c for c in page["items"] if str(c.get("product_id")) not in seen]
    items = left if req.limit == 0 else left[:req.limit]
    return {"items": items, "currency": page.get("currency"),
            "total": page.get("total", len(page["items"])), "remaining": len(left) - len(items)}


@router.get("/support/categories")
async def support_categories(
    limit: int = Query(default=shopify_storefront.CATEGORY_LIMIT, ge=1, le=shopify_storefront.CATEGORY_LIMIT),
) -> dict:
    """Every category a shopper can buy from, each with a picture to draw it with.

    A category is the product type Shopify stores on the product - Dress, Boots,
    Romper. Only ACTIVE products are grouped, so a tile never opens onto a draft
    or an archived line, and the biggest categories come first.

    Categories only - no product fields, and no product wording. The picture is
    necessarily borrowed from a product, because a product type has no image of
    its own, but nothing else about that product comes with it.

    Each entry carries:
      id            a stable url-safe slug of the name ("dress"), the value to
                    send back when filtering by this category
      name          the category exactly as the store spells it ("Dress")
      image         a photo representing the category, or null if none is available
      image_alt     the category name, for the photo
      url           the store's catalogue filtered to this category
      product_count how many buyable products are in it
      taxonomy_id   Shopify's own category id, where the products carry one -
      taxonomy_name and its name. Both null when the store has not set them.

    Cached, so a client may call it on every page load.
    """
    try:
        return await shopify_storefront.categories(limit)
    except ShopifyError as exc:
        logger.warning("Could not group the catalogue into categories: %s", exc)
        raise HTTPException(status_code=502, detail="The store catalogue could not be reached.") from exc


@router.get("/support/collections")
async def support_collections(
    limit: int = Query(default=shopify_storefront.COLLECTION_LIMIT, ge=1,
                       le=shopify_storefront.COLLECTION_LIMIT),
) -> dict:
    """The store's collections, each with a picture to draw it with.

    These are the collections the storefront's own /collections page lists, in
    the same alphabetical order, so the widget and the shop agree. Collections
    with nothing in them are left out - they are a dead end for a shopper.

    Each entry carries:
      id            the collection handle ("spring-bloom"), the value to send
                    back when filtering by it
      name          the collection as the store spells it ("Spring Bloom")
      handle, title the same two under Shopify's own names
      image         the collection's image, falling back to a product photo from
                    inside it, or null if neither exists
      image_alt     the collection name, for the photo
      url           the collection on the storefront
      product_count how many products are in it

    Set SUPPORT_WELCOME_COLLECTIONS to pin an exact, ordered subset; without it
    every published collection comes back. Cached, so a client may call it on
    every page load.
    """
    try:
        return await shopify_storefront.collections(limit, _welcome_handles())
    except ShopifyError as exc:
        logger.warning("Could not read the store's collections: %s", exc)
        raise HTTPException(status_code=502, detail="The store collections could not be reached.") from exc


@router.post("/support/size")
async def support_size(req: SizeRequest) -> dict:
    """Smart Size Finder: the quiz's answers in, one recommended size out.

    Returns recommended ("6-7Y"), age_label, fit (close | true | roomy),
    fit_note, alternatives (the sizes either side, as sold), and the product
    with its variants so the widget can add exactly that size to the bag.
    found=false lists what is still_to_ask.
    """
    try:
        return await size_finder.for_product(
            req.product, age=req.age, height_cm=req.height_cm,
            chest_cm=req.chest_cm, usual_size=req.usual_size,
        )
    except ShopifyError as exc:
        logger.warning("Size finder could not read the product: %s", exc)
        raise HTTPException(status_code=502, detail="The store catalogue could not be reached.") from exc


@router.get("/support/topup")
async def support_topup(
    country: str = Query(default="", max_length=2),
    currency: str = Query(default="", max_length=3),
    limit: int = Query(default=2, ge=1, le=4),
) -> dict:
    """A couple of small pieces that would take a bag to the next discount tier.

    The cheapest in-stock accessories first - what the deck calls "add 1 more
    for 10% off". Priced for the shopper's own market like everything else.
    """
    token = market.set_country(country)
    showing = market.set_showing(currency)
    try:
        catalogue = await outfit.browse_catalogue()
        stock = [p for p in catalogue["products"] if p["in_stock"] and p["price_from"]]
        small = sorted(stock, key=lambda p: (0 if p["category"] in ("Accessory", "Socks") else 1, p["price_from"]))
        picks = []
        for p in small[:limit]:
            # The + button adds straight to the bag, so each pick carries the
            # variant it would add - which is also what gets the market price.
            node = await size_finder.find_product(p["handle"])
            variant = next((v for v in ((node or {}).get("variants") or {}).get("nodes") or []
                            if v.get("availableForSale")), None)
            picks.append({
                "product_id": p.get("product_id"), "handle": p["handle"], "title": p["title"],
                "variant_id": (variant or {}).get("legacyResourceId"),
                "price": p["price_from"], "price_from": p["price_from"],
                "currency": catalogue["currency"],
                "image": p.get("image"), "url": p.get("url"),
            })
        return await market.localize({"currency": catalogue["currency"], "products": picks})
    except ShopifyError as exc:
        logger.warning("Could not read the catalogue for a top-up: %s", exc)
        return {"products": []}
    finally:
        market.reset_country(token)
        market.reset_showing(showing)


@router.get("/support/offer")
async def support_offer() -> dict:
    """The store's multi-item tiers, read from its own automatic discounts.
    Empty when none are set up - the widget then shows no offer."""
    return {"tiers": await multi_buy.tiers()}


class StateRequest(BaseModel):
    """The widget saving or loading a signed-in shopper's own chat state."""

    customer: Customer | None = None
    # Absent: a load. Present: a save, and the stored state is replaced with it.
    state: dict | None = None


STATE_LIMIT = 300_000          # characters of JSON - a few chats with their cards


@router.post("/support/state")
async def support_state(req: StateRequest) -> dict:
    """A signed-in shopper's recents, saved pieces and remembered details.

    Only for a shopper the theme has signed (SUPPORT_CUSTOMER_SIGNING_SECRET):
    the key is their signed customer id, so nobody can read or overwrite anyone
    else's. Guests keep everything in their own browser and never reach here.
    """
    shopper = identity.resolve(req.customer)
    if shopper is None or not shopper.customer_id:
        return {"synced": False, "reason": "not_signed_in"}
    key = f"customer:{shopper.customer_id}"
    async with AsyncSessionLocal() as db:
        row = await db.get(ShopperState, key)
        if req.state is None:
            return {"synced": True, "state": (row.data if row else None)}
        if len(json.dumps(req.state, ensure_ascii=False)) > STATE_LIMIT:
            raise HTTPException(status_code=413, detail="That is more than we keep for one shopper.")
        if row:
            row.data = req.state
        else:
            db.add(ShopperState(customer_key=key, data=req.state))
        await db.commit()
    return {"synced": True}


@router.get("/support/history")
async def support_history(session_id: str = Query(..., min_length=10, max_length=64)) -> dict:
    """A support conversation's text, oldest first, to reopen it from the sidebar.

    Session ids are random and only ever handed to the browser that started the
    conversation, so holding one is what entitles you to read it back. Product
    cards are not stored - the text is.
    """
    if not session_id.startswith(SESSION_PREFIX):
        raise HTTPException(status_code=404, detail="No such conversation.")
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id.asc())
                .limit(60)
            )
        ).scalars().all()
    turns = [(m.role, _without_cards_note(m.content)) for m in rows]
    return {
        "session_id": session_id,
        "messages": [{"role": r, "content": c} for r, c in turns],
        "understood": needs.understood([c for r, c in turns if r == "user"]),
    }


@router.post("/support/chat")
async def support_chat(req: SupportChatRequest) -> StreamingResponse:
    """Chat with the customer support agent. Replies stream back as SSE.

    An empty ``message`` is the storefront widget opening rather than a question:
    it answers with the greeting and the store's collections, saves nothing to
    the thread, and never reaches the agent.

    Events, each with a JSON payload:
      session - {"session_id", "agent"}          first, so the client can keep the thread
      token   - {"text"}                         a piece of the reply
      reset   - {}                               clear the reply shown so far; the agent
                                                 was thinking out loud before a lookup
      tool    - {"name", "phase"}                the agent is looking something up
      cart    - {items[], currency, total,       the shopper's own cart, with an image
                 item_count}                      and link added to every line
      orders  - {orders[]}                    past or looked-up orders, each line
                                                 with its image, link and variant
      collections - {count, collections[]}       welcome screen only: each has
                                                 handle, title, image, url and
                                                 product_count
      products- {items[], currency}               product cards to render: each has
                                                 product_id, variant_id, title, option,
                                                 price, image and url
      outfit  - {items[], currency, total,        a complete look: the same cards plus
                 budget, within_budget,           the exact total and the variants to
                 cart_items[]}                    add to the bag
      action  - {type: "add_to_cart", items[]}   the agent's own add or redirect, with
                | {type: "redirect", page, url}  exact variant ids; one per call it made.
                | {type: "confirm_add", items[]} Nothing is added: a checklist of these
                                                 lines for the shopper to keep or change
      done    - {"session_id", "reply",          the finished reply, repeating
                 products?, outfit?,             whatever cards were produced, the
                 greeting?, collections?,        `action` list
                 actions?}
      error   - {"message"}                      the turn failed; nothing was saved
      understood - {fields[], age}               what the shopper has asked for so far
                                                 (age, occasion, budget, size...), for the
                                                 "Understood" panel and search chips
      size    - {recommended, fit_note, ...}     a Smart Size Finder answer, as /support/size
      welcome_back - {first_name,                welcome screen, verified shoppers only:
                 previously_bought[], picks}     past purchases and what goes with them
    """
    session_id = _resolve_session(req.session_id)
    history = [] if not req.message.strip() else await _load_history(session_id, req.message)
    # The briefing rides along with this turn only; history keeps the raw message.
    briefing = describe(req.cart, req.customer, req.context)
    # What shop this is, on every turn. Working blind is what made the answers
    # read as guesses; it is cached, so it costs a few tokens and no lookups.
    try:
        about = await store_profile.facts()
        briefing = f"{briefing}\n\n{about}" if briefing else about
    except Exception:  # noqa: BLE001 - a fact sheet must never cost the answer
        logger.warning("Could not read the store facts for this turn", exc_info=True)
    if _is_greeting(req.message):
        store = await _store_briefing(req.customer)
        briefing = f"{briefing}\n\n{store}" if briefing else store
    # Told outright rather than left for the model to count off the message: it
    # was reading "2 jackets" and still showing the shelf.
    remembered = {k: str(v)[:40] for k, v in (req.profile or {}).items() if k in needs.REMEMBERED}
    if remembered and req.message.strip():
        told = "; ".join(f"{needs.LABELS[k]}: {v}" for k, v in remembered.items())
        memo = (f"[Remembered from their earlier visits: {told}. Use these unless they say otherwise; "
                "do not ask again for what is here. If they now describe a different child (another "
                "age), these belong to the earlier one: do not apply or mention them.]")
        briefing = f"{briefing}\n{memo}" if briefing else memo
    shopper = identity.resolve(req.customer)
    if shopper is not None:
        who = f"[Signed in and verified: {shopper.first_name or 'a returning customer'} - past orders and picks are available]"
        briefing = f"{briefing}\n{who}" if briefing else who
    bag_offer = None
    if req.cart and req.cart.items:
        bag_offer = multi_buy.summary(await multi_buy.tiers(), req.cart.item_count,
                                      shopify_storefront.minor_to_major(req.cart.total_price),
                                      req.cart.currency)
        if line := multi_buy.headline(bag_offer):
            briefing = f"{briefing}\nMulti-item offer on their bag: {line}"

    async def events() -> AsyncIterator[str]:
        # Every price in this stream - the welcome tiles as much as the agent's
        # cards - is the one the shopper's own storefront is quoting.
        market.set_country(req.context.country if req.context else None)
        market.set_showing(req.context.currency if req.context else None)
        yield _sse("session", {"session_id": session_id, "agent": CUSTOMER_SUPPORT_AGENT.name})

        # What they have asked for so far - age, occasion, budget, size - drawn by
        # the widget as the "Understood" panel and the "Searching for" chips.
        # Which of this store's tags mean Boys, Girls or Baby - read by the model
        # once and cached, so every product card says whose it is.
        try:
            await audience.ensure()
        except Exception:  # noqa: BLE001 - exact tag names are the fallback
            logger.warning("Could not prepare the audience tags", exc_info=True)
        turn_understood = None
        if req.message.strip():
            # Everything they have told us in this chat, not just what the model
            # replays: the 8-row window lost the age and budget after 4 turns.
            said = (await _user_messages(session_id)) + [req.message]
            understood = await understanding.understood(
                said, base=remembered, currency=(req.context.currency if req.context else None))
            # Who they are shopping for, so a mixed collection comes back as
            # theirs rather than half somebody else's.
            identity.set_audience(next(
                (f["value"] for f in understood["fields"] if f["key"] == "for"), None))
            identity.set_colour(next(
                (f["value"] for f in understood["fields"] if f["key"] == "colour"), None))
            identity.set_size(next(
                (f["value"] for f in understood["fields"] if f["key"] == "size"), None))
            identity.set_age(understood.get("age"))
            turn_understood = understood
            identity.set_season(next(
                (f["value"] for f in understood["fields"] if f["key"] == "season"), None))
            await _budget_in_their_money(
                understood, req.context.currency if req.context else None)
            identity.set_budget(_budget_amount(
                understood, req.context.currency if req.context else None))
            if understood["fields"]:
                yield _sse("understood", understood)

        # The widget sends the cart without imagery, so hand it straight back with
        # pictures and links. Sent before the reply so the panel can draw at once.
        cart_payload: dict | None = None
        if req.cart and req.cart.items:
            try:
                cart_payload = {
                    "items": await shopify_storefront.cart_cards(
                        [line.model_dump() for line in req.cart.items], req.cart.currency
                    ),
                    "currency": req.cart.currency,
                    "item_count": req.cart.item_count,
                    "total": shopify_storefront.minor_to_major(req.cart.total_price),
                    "multi_buy": bag_offer,
                }
                yield _sse("cart", cart_payload)
            except Exception:
                logger.warning("Could not decorate the cart for session %s", session_id, exc_info=True)

        # An empty message is the widget opening, not a question. Greet, offer the
        # collections to tap, and never spend an agent turn on it.
        if not req.message.strip():
            greeting = await _welcome_text()
            yield _sse("token", {"text": greeting})
            welcome: dict = {"greeting": greeting}
            try:
                found = await shopify_storefront.collections(
                    settings.SUPPORT_WELCOME_COLLECTION_LIMIT, _welcome_handles()
                )
                welcome["collections"] = found["collections"]
                yield _sse("collections", found)
            except Exception:  # noqa: BLE001 - show the greeting even with no collections
                logger.warning("Could not load the welcome collections", exc_info=True)
                welcome["collections"] = []
            try:
                chips = await suggestions.for_welcome()
                welcome["suggestions"] = chips
                yield _sse("suggestions", {"suggestions": chips})
            except Exception:  # noqa: BLE001 - chips are a nicety, never a blocker
                logger.warning("Could not build welcome suggestions", exc_info=True)
                welcome["suggestions"] = []
            if shopper is not None:
                try:
                    back = await market.localize(await _welcome_back(shopper))
                    if back:
                        welcome["welcome_back"] = back
                        yield _sse("welcome_back", back)
                except Exception:  # noqa: BLE001 - a greeting must not fail on order history
                    logger.warning("Could not build the welcome-back panel", exc_info=True)
            try:
                # A budget worth suggesting, in this visitor's own money: three
                # pieces at what our pieces actually cost, not a number typed
                # into the theme in pounds.
                catalogue = await outfit.browse_catalogue()
                ids = [p["product_id"] for p in (catalogue.get("products") or [])
                       if p.get("product_id") and p.get("price_from")][:15]
                if hint := await market.typical_spend(ids):
                    welcome["budget_hint"] = hint
                    yield _sse("budget_hint", hint)
            except Exception:  # noqa: BLE001 - a suggested figure is a nicety
                logger.warning("Could not work out a budget hint", exc_info=True)
            welcome["offer"] = {"tiers": await multi_buy.tiers()}
            welcome = await market.localize(welcome)
            done_payload = {"session_id": session_id, "reply": greeting, **welcome}
            if cart_payload:
                done_payload["cart"] = cart_payload
            yield _sse("done", done_payload)
            return

        reply = ""
        declared = None
        cards = CardCollector()
        token = identity.set_current(shopper)
        session_token = identity.set_session(session_id)
        cart_token = identity.set_cart(req.cart)
        screen_token = identity.set_screen(
            req.context.cards_on_screen if req.context else None,
            req.context.viewing_product if req.context else None)
        saved_token = identity.set_saved(req.saved)
        # What the shopper has actually written, so that nothing can be put in
        # their bag unless the agent quotes the words that asked for it. Their
        # last few turns, not just this one: "add it" and the size that answers
        # our question about it arrive one message apart.
        said_token = identity.set_said(
            [content for role, content in history[-6:] if role == "user"] + [req.message])
        country_token = market.set_country(req.context.country if req.context else None)
        showing_token = market.set_showing(req.context.currency if req.context else None)
        try:
            # What was read from the chat so far - the same facts the Understood
            # panel shows - so the agent does not ask "boy or girl?" after
            # "a gift for him".
            turn_briefing = briefing
            if turn_understood and turn_understood.get("fields"):
                told = "; ".join(f'{f["label"]}: {f["value"]}' for f in turn_understood["fields"])
                note = (f"[Understood from the chat so far ('For' is who the purchase is for): {told}. "
                        "Use these; do not ask for them again.]")
                turn_briefing = f"{turn_briefing}\n{note}" if turn_briefing else note
            # How many they asked to see, as the model read it - never an age
            # mistaken for a count ("my daughter is 4" once meant 4 items).
            if n := (turn_understood or {}).get("count"):
                ask = f"[They asked for exactly {n} item(s): choose and name exactly {n}, no more]"
                turn_briefing = f"{turn_briefing}\n{ask}" if turn_briefing else ask
            asking = with_context(req.message, turn_briefing)
            # A second pass only when the first named products without calling a
            # single tool - see _named_without_looking. A pass that called any
            # tool is never re-run: it may have put something in their bag.
            for attempt in range(2):
                used_tools = False
                async for event in CUSTOMER_SUPPORT_AGENT.stream(asking, history):
                    if event["type"] == "token":
                        yield _sse("token", {"text": event["text"]})
                    elif event["type"] == "reset":
                        yield _sse("reset", {})
                    elif event["type"] == "tool":
                        used_tools = True
                        yield _sse("tool", {"name": event["name"], "phase": event["phase"],
                                            **({"input": event["input"]} if event.get("input") else {})})
                        if event["phase"] == "end":
                            # Collected now, sent once the reply exists - see finalise().
                            cards.take(event["name"], event.get("output"))
                    elif event["type"] == "final":
                        reply, declared = split_show(_without_cards_note(event["reply"]))
                if attempt or used_tools or not await _named_without_looking(
                        reply, req.cart, req.context.cards_on_screen if req.context else None):
                    break
                logger.info("Session %s: reply named products without a lookup; asking again", session_id)
                # Take the unlooked-up draft off the shopper's screen.
                yield _sse("reset", {})
                asking = f"{asking}\n\n{_LOOK_IT_UP}"
        except Exception:
            logger.exception("Support chat failed for session %s", session_id)
            yield _sse("error", {"message": "Sorry — something went wrong. Please try again."})
            return
        finally:
            identity.reset(token)
            identity.reset_session(session_token)
            identity.reset_cart(cart_token)
            identity.reset_screen(screen_token)
            identity.reset_saved(saved_token)
            identity.reset_said(said_token)
            market.reset_country(country_token)
            market.reset_showing(showing_token)

        # Repeated in `done` so a client that only reads the final event still
        # gets the cards without having to follow the stream.
        # Has the shopper narrowed things (colour, age, size, budget) - in this
        # message or earlier in the chat? If not, a category browse is shown whole
        # rather than trimmed to the names said. Earlier turns count: "yes, show
        # me" after giving his age and budget put baby bonnets and a 20000 jacket
        # under a reply that had picked out the pieces in his size.
        # What the model read for the panel this turn, when it read anything.
        asked_for = {f["key"] for f in (turn_understood or {}).get("fields") or []}
        try:
            names_a_kind = await _names_a_kind(req.message)
        except Exception:  # noqa: BLE001 - a hint for card trimming, never worth the reply
            logger.warning("Could not read the kind of piece for session %s", session_id, exc_info=True)
            names_a_kind = False
        narrowed = bool(asked_for & {"colour", "age", "size", "budget", "occasion", "style"}) or names_a_kind
        # A size shelf is only cut down by something beyond the size itself.
        past_size = bool(asked_for & {"colour", "budget", "occasion", "style"}) or names_a_kind
        # The reply is already written: a failure choosing its cards or saving the
        # turn must not end the stream without it (the widget shows text only on
        # `done`, so the shopper would see nothing at all).
        try:
            cards.finalise(reply, narrowed=narrowed, narrowed_past_size=past_size, declared=declared)
            cards.limit_products((turn_understood or {}).get("count"))
            if req.cart is not None and not req.cart.items:
                cards.drop_empty_checkout()
            drawn = cards.as_dict()
        except Exception:  # noqa: BLE001
            logger.exception("Could not finish the cards for session %s", session_id)
            drawn = {}
        # Saved as the shopper read it. A note of the cards used to ride along
        # here and the agent copied it into its next reply; the widget now sends
        # the cards on screen with every message instead (cards_on_screen).
        try:
            await _save_turn(session_id, req.message, _without_cards_note(reply))
        except Exception:  # noqa: BLE001
            logger.exception("Could not save the turn for session %s", session_id)
        for name, payload in drawn.items():
            yield _sse(name, payload)
        # The widget carries these out, in order: add these variants to the bag,
        # take them to checkout. Repeated in `done` for a client that only reads
        # the last event - carry each out once, from one place or the other.
        for action in cards.actions:
            yield _sse("action", action)
        # Only the agent's own actions touch the bag. The keyword net that used to
        # sit here ("add it" -> add every card shown, in its default size) acted
        # on words the agent had already judged, and put wrong sizes in bags.

        try:
            chips = await suggestions.for_turn(
                cards.category, reply=reply, shown_products=cards.shown_products()
            )
        except Exception:  # noqa: BLE001 - never fail a reply over a chip row
            logger.warning("Could not build suggestions for session %s", session_id, exc_info=True)
            chips = []
        if chips:
            yield _sse("suggestions", {"suggestions": chips})

        done_payload = {"session_id": session_id, "reply": reply, **drawn}
        if chips:
            done_payload["suggestions"] = chips
        if cart_payload:
            done_payload["cart"] = cart_payload
        if cards.actions:
            done_payload["actions"] = cards.actions
        yield _sse("done", done_payload)

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)
