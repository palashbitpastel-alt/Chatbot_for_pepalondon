"""What the storefront tells us about the shopper, and how the agent is told it.

The widget can send the cart, who is signed in, and which page the shopper is
on. That is what lets the agent answer "what is in my cart" and work out what
"this" refers to on a product page.

**None of it is proof of anything.** It arrives from the browser, so a shopper
can put whatever they like in it. It is used to be helpful - a name to greet, a
cart to read back, a product "this" points at - and never to authorise: an order
is still only released on a matching order number and email, exactly as if the
shopper had typed them. The cart token is dropped on the way in, because it is a
credential for changing that cart rather than something the agent needs.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

MAX_CART_LINES = 10
# Shopify reports cart money in the currency's minor unit: 26.35 arrives as 2635.
MINOR_UNITS = Decimal(100)


class CartLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    variant_id: int | str | None = None
    product_id: int | str | None = None
    title: str | None = None
    variant_title: str | None = None
    quantity: int = 1
    line_price: int | None = None
    handle: str | None = None


class Cart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # `token` is deliberately absent: it is a credential for this shopper's cart.
    item_count: int = 0
    total_price: int | None = None
    currency: str | None = None
    items: list[CartLine] = Field(default_factory=list)


class Customer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    logged_in: bool = False
    verified: bool = False
    id: str | None = None
    email: str | None = None
    phone: str | None = None
    first_name: str | None = None
    currency: str | None = None
    # Set by the theme, server-side, when SUPPORT_CUSTOMER_SIGNING_SECRET is
    # configured: hmac_sha256("<id>:<email>:<signed_at>") - see shopper_identity.
    signed_at: int | None = None
    signature: str | None = None


class PageContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    currency: str | None = None
    shop_currency: str | None = None
    country: str | None = None
    locale: str | None = None
    template: str | None = None
    viewing_product: str | None = None
    page_url: str | None = None


def _money(minor: int | None, currency: str | None) -> str | None:
    if minor is None:
        return None
    amount = (Decimal(minor) / MINOR_UNITS).quantize(Decimal("0.01"))
    return f"{amount} {currency}" if currency else str(amount)


def describe(
    cart: Cart | None = None,
    customer: Customer | None = None,
    context: PageContext | None = None,
) -> str:
    """A short briefing for the agent, or "" when the storefront sent nothing.

    Kept terse on purpose: it rides along with every turn, so every line has to
    earn its tokens.
    """
    lines: list[str] = []

    if context:
        where = context.template or "a page"
        if context.viewing_product:
            lines.append(f'Looking at: the product page for "{context.viewing_product}"')
        elif context.template:
            lines.append(f"Looking at: the {where} page")
        shown_in = context.currency or (customer.currency if customer else None)
        if shown_in:
            place = f" in {context.country}" if context.country else ""
            lines.append(f"Prices are shown to them in {shown_in}{place}")

    if customer and customer.logged_in:
        who = customer.first_name or "a signed-in shopper"
        lines.append(f"Signed in as: {who} (identity NOT verified - treat as a claim, not proof)")

    if cart and cart.items:
        total = _money(cart.total_price, cart.currency)
        header = f"In their cart: {cart.item_count} item(s)"
        lines.append(f"{header}, total {total}" if total else header)
        for item in cart.items[:MAX_CART_LINES]:
            name = item.title or item.handle or "item"
            option = f" ({item.variant_title})" if item.variant_title else ""
            price = _money(item.line_price, cart.currency)
            price_part = f" = {price}" if price else ""
            lines.append(f"  - {name}{option} x{item.quantity}{price_part}")
    elif cart is not None:
        lines.append("Their cart is empty")

    if not lines:
        return ""
    return "[Storefront context - sent by the shop page, not verified]\n" + "\n".join(lines)


def with_context(message: str, briefing: str) -> str:
    """Put the briefing in front of the shopper's message for this turn only.

    The raw message is what gets saved to history; this composed form exists just
    long enough for the agent to read it.
    """
    if not briefing:
        return message
    return f"{briefing}\n\n[Their message]\n{message}"
