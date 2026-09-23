"""Who the agent is allowed to treat as the shopper, for this request only.

Order history is bulk personal data: one address returns everything that person
has ever bought. The storefront can tell us who is signed in, but that block
comes from the browser, so on its own it is a claim, not proof - anyone could
POST somebody else's address and read their history.

So the identity lives in a context variable set by the endpoint, never in a tool
argument. The agent cannot pass an email to the history tools even if a shopper
talks it into trying: it can only ask about *the* shopper, and the request has
already decided who that is. When nothing is trusted, those tools decline and
the ordinary order-number-plus-email flow still works.

Trust comes from one of two places. A signed block: the theme computes an HMAC
over the customer's id, email and a timestamp with a secret the browser never
sees (``SUPPORT_CUSTOMER_SIGNING_SECRET``), so a forged or edited block fails the
check. Or ``settings.TRUST_STOREFRONT_CUSTOMER``, which trusts the bare claim and
should stay off on a public endpoint.
"""

import hashlib
import hmac
import time
from contextvars import ContextVar
from dataclasses import dataclass

from app.core.config import settings


@dataclass(frozen=True)
class Shopper:
    """A shopper the request has established we may act for."""

    email: str
    first_name: str | None = None
    customer_id: str | None = None


_current: ContextVar[Shopper | None] = ContextVar("current_shopper", default=None)

# The conversation this turn belongs to. Order-change tickets are bound to it,
# so a token that leaks out of one transcript cannot be spent in another.
_session: ContextVar[str | None] = ContextVar("current_session", default=None)


def resolve(customer, trusted_email: str | None = None) -> Shopper | None:
    """Decide who, if anyone, this request may look up.

    ``trusted_email`` is for a caller that has authenticated the shopper itself
    (a signed App Proxy request, say) and always wins. Otherwise the storefront's
    own claim is used only when the deployment has opted into trusting it.
    """
    if trusted_email:
        return Shopper(email=trusted_email.strip().casefold(),
                       first_name=getattr(customer, "first_name", None))
    if customer is None or not customer.email:
        return None
    if not customer.logged_in:
        return None
    if not (settings.TRUST_STOREFRONT_CUSTOMER or signature_valid(customer)):
        return None
    return Shopper(
        email=customer.email.strip().casefold(),
        first_name=customer.first_name,
        customer_id=str(customer.id) if customer.id else None,
    )


def signature_valid(customer, now: float | None = None) -> bool:
    """Whether the theme really signed this customer block, recently.

    The theme signs "<id>:<email lowercased>:<signed_at>" with Liquid's
    hmac_sha256 filter and the shared secret. Any edit to the id or email, a
    stale timestamp, or no secret configured at all, and this is False.
    """
    secret = settings.SUPPORT_CUSTOMER_SIGNING_SECRET
    signature = getattr(customer, "signature", None)
    signed_at = getattr(customer, "signed_at", None)
    if not (secret and signature and signed_at and customer.id and customer.email):
        return False
    age = (now or time.time()) - int(signed_at)
    if age < -300 or age > settings.SUPPORT_CUSTOMER_SIGNATURE_MAX_AGE_HOURS * 3600:
        return False
    message = f"{customer.id}:{customer.email.strip().lower()}:{int(signed_at)}"
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def set_current(shopper: Shopper | None):
    """Bind the shopper for this turn. Returns a token for ``reset``."""
    return _current.set(shopper)


def reset(token) -> None:
    _current.reset(token)


def current() -> Shopper | None:
    return _current.get()


def set_session(session_id: str | None):
    """Bind the conversation for this turn. Returns a token for ``reset_session``."""
    return _session.set(session_id)


def reset_session(token) -> None:
    _session.reset(token)


def current_session() -> str | None:
    return _session.get()


# The shopper's bag as the storefront sent it this turn, so the cart tools can
# match "the plimsolls" to an exact line. It is only ever used to tell the
# browser which of ITS OWN lines to change - the storefront does the change.
_cart: ContextVar[object | None] = ContextVar("current_cart", default=None)


def set_cart(cart) -> object:
    return _cart.set(cart)


def reset_cart(token) -> None:
    _cart.reset(token)


def current_cart():
    return _cart.get()


# The shopper's wishlist as the widget holds it (it lives in their browser), so
# "show my saved" and "remove the bonnet from my saved" can be answered.
_saved: ContextVar[list | None] = ContextVar("current_saved", default=None)


def set_saved(items) -> object:
    return _saved.set(list(items or []))


def reset_saved(token) -> None:
    _saved.reset(token)


def current_saved() -> list:
    return _saved.get() or []


# Who they are shopping for, as the conversation has established it ("For: Girl"
# in the Understood panel). A shopper who has said "my daughter" and then taps a
# mixed collection should not be handed boys' trousers, and the model cannot be
# relied on to remember to filter - so the tools do it.
_audience: ContextVar[str | None] = ContextVar("shopping_for", default=None)


def set_audience(who: str | None) -> object:
    known = {"girl": "Girls", "girls": "Girls", "boy": "Boys", "boys": "Boys",
             "baby": "Baby", "babies": "Baby"}
    return _audience.set(known.get((who or "").strip().lower()))


def reset_audience(token) -> None:
    _audience.reset(token)


def shopping_for() -> str | None:
    return _audience.get()


# The colour they asked for, for the same reason as the audience above: told
# "blue", a shopper should not be handed a shelf of camel and burgundy.
_colour: ContextVar[str | None] = ContextVar("wants_colour", default=None)


def set_colour(colour: str | None) -> object:
    name = " ".join((colour or "").strip().lower().split())
    return _colour.set(name or None)


def reset_colour(token) -> None:
    _colour.reset(token)


def wants_colour() -> str | None:
    return _colour.get()


# The size the conversation has settled on ("Size 8Y" in the Understood panel).
# Without it a look falls back to the first size a piece is sold in, which is
# the smallest - a six year old was dressed in 12M and handed a dummy.
_size: ContextVar[str | None] = ContextVar("wants_size", default=None)


def set_size(size: str | None) -> object:
    return _size.set((size or "").strip() or None)


def reset_size(token) -> None:
    _size.reset(token)


def wants_size() -> str | None:
    return _size.get()


# The season they are shopping for. "It's summer now" has to keep the wool
# coats out of the answer, the same way a colour keeps the wrong ones out.
_season: ContextVar[str | None] = ContextVar("wants_season", default=None)


def set_season(season: str | None) -> object:
    return _season.set((season or "").strip().title() or None)


def reset_season(token) -> None:
    _season.reset(token)


def shopping_season() -> str | None:
    return _season.get()
