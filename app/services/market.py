"""Prices in the shopper's own market.

A store sells in several markets - India in rupees, the UK in pounds - and each
market has its own price list and rounding (a $23.27 bodysuit is ₹2,300 in
India, £18 in the UK). The catalogue reads return the base price, so every tool
result that carries prices is passed through ``localize`` before the agent or
the storefront sees it: Shopify is asked for each variant's contextual price in
the shopper's country, and prices, totals, currency labels and comparison
summaries are rewritten to match. Nothing here converts with an exchange rate;
every figure is the market's own.
"""

import copy
import logging
import time
from contextvars import ContextVar
from decimal import ROUND_HALF_UP, Decimal

from app.services import compare
from app.services.shopify_client import ShopifyError, graphql

logger = logging.getLogger(__name__)

_country: ContextVar[str | None] = ContextVar("shopper_country", default=None)
# What the storefront itself is showing. A market price is only right if it is
# in that money: a browser that reports Austria while the shop is serving its
# US market would otherwise be quoted euros beside dollar cart lines.
_showing: ContextVar[str | None] = ContextVar("storefront_currency", default=None)

PRICES = """
query MarketPrices($ids: [ID!]!, $country: CountryCode!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      contextualPricing(context: {country: $country}) {
        price { amount currencyCode }
        compareAtPrice { amount }
      }
    }
    ... on Product {
      id
      contextualPricing(context: {country: $country}) {
        minVariantPricing { price { amount currencyCode } }
        maxVariantPricing { price { amount currencyCode } }
      }
    }
  }
}
"""
CACHE_SECONDS = 600
BATCH = 100
_cache: dict[tuple[str, str], tuple[float, dict]] = {}

VARIANT_PRICE_KEYS = ("price", "unit_price")


# Which country to ask Shopify about, to see what it charges in a given money.
CURRENCY_COUNTRY = {
    "GBP": "GB", "USD": "US", "EUR": "DE", "JPY": "JP", "AUD": "AU", "CAD": "CA",
    "AED": "AE", "SGD": "SG", "CHF": "CH", "SEK": "SE", "NZD": "NZ", "HKD": "HK",
    "ZAR": "ZA", "INR": "IN",
}
SYMBOL_CURRENCY = {"£": "GBP", "$": "USD", "€": "EUR", "¥": "JPY", "₹": "INR",
                   "GBP": "GBP", "USD": "USD", "EUR": "EUR", "INR": "INR", "AED": "AED"}


def _round_money(amount: Decimal) -> float:
    """A budget is a round number in the shopper's head; it should stay one."""
    step = Decimal("100") if amount >= 1000 else Decimal("10") if amount >= 100 else Decimal("1")
    return float((amount / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step)


async def in_our_money(amount: float, their_money: str, product_ids) -> dict | None:
    """What a budget in someone else's money buys here, priced off our own shelves.

    "Around £400" from a shopper being quoted rupees used to be answered with a
    question, because there was no exchange rate to hand. There does not need to
    be one: the shop already sells the same piece in both markets, and what it
    charges for it in each IS the rate it trades at. No external feed, no
    invented number - two price lists we were reading anyway.
    """
    here = current_country()
    code = SYMBOL_CURRENCY.get((their_money or "").strip(), (their_money or "").strip().upper())
    there = CURRENCY_COUNTRY.get(code)
    if not here or not there or here == there or not amount:
        return None
    ids = {str(i) for i in (product_ids if isinstance(product_ids, (list, set, tuple))
                            else [product_ids]) if i}
    if not ids:
        return None
    try:
        ours = await _prices("Product", ids, here)
        theirs = await _prices("Product", ids, there)
    except (ShopifyError, KeyError) as exc:
        logger.warning("No implied rate for %s -> %s: %s", code, here, exc)
        return None

    def low(table, pid):
        price = (table.get(pid) or {}).get("minVariantPricing", {}).get("price")
        if not price:
            return None, None
        return Decimal(str(price["amount"])), price["currencyCode"]

    # Each market rounds its own prices, so one piece alone gives a slightly
    # different answer from the next (125 against a neighbour's 127). Take the
    # middle of several and a single oddly-priced line cannot skew a budget.
    rates, our_code, their_code = [], None, None
    for pid in ids:
        mine, mine_code = low(ours, pid)
        yours, yours_code = low(theirs, pid)
        if not mine or not yours or yours <= 0 or yours_code != code:
            continue
        rates.append(mine / yours)
        our_code, their_code = mine_code, yours_code
    if not rates:
        return None
    rates.sort()
    middle = rates[len(rates) // 2] if len(rates) % 2 else \
        (rates[len(rates) // 2 - 1] + rates[len(rates) // 2]) / 2
    return {
        "their_budget": float(amount), "their_currency": their_code,
        "our_budget": _round_money(Decimal(str(amount)) * middle),
        "our_currency": our_code,
        "priced_from": len(rates),
        "how": (f"what this shop itself charges in each market, across {len(rates)} "
                f"pieces - about {middle.quantize(Decimal('0.01'))} {our_code} "
                f"to the {their_code}"),
    }


async def typical_spend(product_ids, pieces: int = 3) -> dict | None:
    """A believable budget in the shopper's own money, read off our own shelves.

    The welcome screen suggested "around £400" to everyone, pounds included, to
    a shopper being quoted rupees. What a look costs is not a constant and not
    ours to invent: it is roughly what a few of our pieces cost, in whatever
    money this visitor is being charged.
    """
    here = current_country()
    ids = {str(i) for i in (product_ids or []) if i}
    if not here or not ids:
        return None
    try:
        priced = await _prices("Product", ids, here)
    except (ShopifyError, KeyError) as exc:
        logger.warning("No typical spend for %s: %s", here, exc)
        return None
    amounts, code = [], None
    for value in priced.values():
        price = (value or {}).get("minVariantPricing", {}).get("price")
        if price:
            amounts.append(Decimal(str(price["amount"])))
            code = price["currencyCode"]
    if not amounts or not code:
        return None
    amounts.sort()
    middle = amounts[len(amounts) // 2]
    return {"amount": _round_money(middle * pieces), "currency": code}


def set_country(code: str | None):
    code = (code or "").strip().upper()
    return _country.set(code if len(code) == 2 and code.isalpha() else None)


def reset_country(token) -> None:
    _country.reset(token)


def set_showing(currency: str | None):
    code = (currency or "").strip().upper()
    return _showing.set(code if len(code) == 3 and code.isalpha() else None)


def reset_showing(token) -> None:
    _showing.reset(token)


def current_country() -> str | None:
    return _country.get()


def _money(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _walk(obj, variants: set, products: set) -> None:
    if isinstance(obj, dict):
        if obj.get("variant_id") and any(obj.get(k) is not None for k in VARIANT_PRICE_KEYS):
            variants.add(str(obj["variant_id"]))
        # A card with no variant of its own - a welcome pick, a saved item - is
        # priced from its product instead.
        if obj.get("product_id") and not obj.get("variant_id") and (
                obj.get("price_from") is not None or obj.get("price") is not None):
            products.add(str(obj["product_id"]))
        for v in obj.values():
            _walk(v, variants, products)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, variants, products)


async def _prices(kind: str, ids: set, country: str) -> dict:
    """{id: pricing} for ProductVariant or Product ids, cached per country."""
    now = time.monotonic()
    out, missing = {}, []
    for i in ids:
        hit = _cache.get((f"{kind}/{i}", country))
        if hit and now - hit[0] < CACHE_SECONDS:
            out[i] = hit[1]
        else:
            missing.append(i)
    for start in range(0, len(missing), BATCH):
        chunk = missing[start:start + BATCH]
        data = await graphql(PRICES, {"ids": [f"gid://shopify/{kind}/{i}" for i in chunk], "country": country})
        for node in data.get("nodes") or []:
            if not node or not node.get("contextualPricing"):
                continue
            key = node["id"].rsplit("/", 1)[-1]
            out[key] = node["contextualPricing"]
            _cache[(f"{kind}/{key}", country)] = (now, node["contextualPricing"])
    return out


def _apply(obj, vp: dict, pp: dict, state: dict) -> None:
    if isinstance(obj, dict):
        vid = str(obj.get("variant_id") or "")
        if vid in vp:
            price = vp[vid]["price"]
            amount = _money(price["amount"])
            state["currency"] = price["currencyCode"]
            for k in VARIANT_PRICE_KEYS:
                if obj.get(k) is not None:
                    obj[k] = amount
            if obj.get("compare_at_price") is not None and vp[vid].get("compareAtPrice"):
                obj["compare_at_price"] = _money(vp[vid]["compareAtPrice"]["amount"])
            if obj.get("line_total") is not None:
                obj["line_total"] = _money(Decimal(str(amount)) * int(obj.get("quantity") or 1))
        pid = str(obj.get("product_id") or "")
        if pid in pp and not obj.get("variant_id"):
            low = pp[pid]["minVariantPricing"]["price"]
            high = pp[pid]["maxVariantPricing"]["price"]
            if obj.get("price_from") is not None:
                obj["price_from"] = _money(low["amount"])
            if obj.get("price_to") is not None:
                obj["price_to"] = _money(high["amount"])
            if obj.get("price") is not None:
                obj["price"] = _money(low["amount"])
            state["currency"] = low["currencyCode"]
        for v in obj.values():
            _apply(v, vp, pp, state)
    elif isinstance(obj, list):
        for v in obj:
            _apply(v, vp, pp, state)


def _relabel(obj, currency: str) -> None:
    if isinstance(obj, dict):
        if "currency" in obj and obj["currency"]:
            obj["currency"] = currency
        for v in obj.values():
            _relabel(v, currency)
    elif isinstance(obj, list):
        for v in obj:
            _relabel(v, currency)


async def localize(result):
    """The same result, priced for the shopper's market. Unchanged when no
    country is known, nothing is priced, or Shopify cannot be asked."""
    country = current_country()
    if not country or not isinstance(result, (dict, list)):
        return result
    original = copy.deepcopy(result)
    variants, products = set(), set()
    _walk(result, variants, products)
    if not variants and not products:
        return result
    try:
        vp = await _prices("ProductVariant", variants, country) if variants else {}
        pp = await _prices("Product", products, country) if products else {}
    except (ShopifyError, KeyError) as exc:
        logger.warning("Market prices for %s unavailable, showing base prices: %s", country, exc)
        return result
    state: dict = {}
    _apply(result, vp, pp, state)
    currency = state.get("currency")
    if not currency:
        return result
    showing = _showing.get()
    if showing and currency != showing:
        # The market we were asked for is not the one the shopper is being
        # served. Their own storefront is the honest answer, so leave it alone.
        logger.info("Storefront is showing %s, not %s - keeping its own prices", showing, currency)
        return original
    _relabel(result, currency)
    if isinstance(result, dict):
        # A look's total is the sum of its localized lines, and its budget was
        # given in the shopper's own money - so judge it in that money too.
        lines = result.get("outfit") or result.get("lines")
        if isinstance(lines, list) and result.get("total") is not None:
            total = sum(Decimal(str(l.get("line_total") or l.get("unit_price") or l.get("price") or 0)) for l in lines)
            result["total"] = _money(total)
            if result.get("budget"):
                result["within_budget"] = float(total) <= float(result["budget"])
        # A comparison's sentences quote prices; rebuild them from the new ones.
        if isinstance(result.get("products"), list) and "difference" in result and len(result["products"]) >= 2:
            try:
                result["in_common"], result["difference"] = compare._contrast(result["products"], currency)
            except (KeyError, TypeError, ValueError):
                logger.debug("Could not rebuild the comparison in %s", currency, exc_info=True)
        result["market"] = {"country": country, "currency": currency}
    return result
