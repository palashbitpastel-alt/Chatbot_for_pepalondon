"""Which store a request is for, so one backend can serve several shops.

The storefront widget sends its shop's permanent myshopify domain with every
call (the ``X-Shop-Domain`` header, or ``?shop=``). The ASGI middleware below
reads it once per request and everything downstream - the Shopify client,
every cache, every saved setting - asks ``current()``.

Only shops on the allowed list are served: the default shop
(SHOPIFY_STORE_URL), SUPPORT_SHOPS, and every shop that installed the app
(saved by the install itself - see services/installs). A request naming any other shop is
refused, and a request naming none is the default shop, so a widget that
predates this keeps working unchanged.

Data kept per shop uses ``key()`` / ``scoped()``: the default shop keeps the
keys it always had, so nothing saved before this is lost; every other shop's
keys carry its domain.
"""

import json
import re
from contextvars import ContextVar
from typing import Any

from app.core.config import settings

_current: ContextVar[str | None] = ContextVar("current_shop", default=None)
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
_OWN_CHECKS = ("/api/v1/shopify/app", "/api/v1/shopify/webhooks")


def normalise(domain: str | None) -> str:
    """"https://Foo.myshopify.com/" -> "foo.myshopify.com"."""
    return (domain or "").strip().lower().removeprefix("https://").removeprefix("http://").strip("/")


def default() -> str:
    return normalise(settings.SHOPIFY_STORE_URL)


def allowed() -> set[str]:
    """The default shop, SUPPORT_SHOPS, and every shop that installed the app."""
    from app.services import installs
    extra = {normalise(d) for d in (settings.SUPPORT_SHOPS or "").split(",") if d.strip()}
    return {d for d in extra | {default()} | installs.installed() if d}


def current() -> str:
    """The shop this request is for; the default shop outside a request."""
    return _current.get() or default()


def is_default() -> bool:
    return current() == default()


def set_current(domain: str | None):
    """Set the shop for the rest of this context. Returns the reset token."""
    return _current.set(normalise(domain) or None)


def key(name: str) -> str:
    """A storage key for this shop. The default shop keeps the bare name."""
    return name if is_default() else f"{current()}|{name}"


def scoped(cache: dict, factory: Any = None) -> Any:
    """This shop's slot in a per-shop cache dict, created on first use."""
    shop = current()
    if shop not in cache and factory is not None:
        cache[shop] = factory()
    return cache.get(shop)


def setting(field: str, default_shop_value: str = "") -> str:
    """A per-shop text setting: name, description, welcome_message,
    welcome_collections, catalogue_filter. The default shop keeps its Railway variables; another
    shop reads SUPPORT_SHOP_SETTINGS, a JSON object keyed by shop domain."""
    if is_default():
        return default_shop_value
    from app.services import installs
    saved = installs.settings_for(current()).get(field)
    if saved:
        return str(saved)
    try:
        profiles = json.loads(settings.SUPPORT_SHOP_SETTINGS or "{}")
    except ValueError:
        return ""
    found = (profiles.get(current()) or {}) if isinstance(profiles, dict) else {}
    value = found.get(field) if isinstance(found, dict) else None
    return str(value) if value else ""


def _requested(scope: dict) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"x-shop-domain":
            return value.decode("latin-1")
    query = scope.get("query_string", b"").decode("latin-1")
    match = re.search(r"(?:^|&)shop=([^&]+)", query)
    return match.group(1) if match else None


class ShopMiddleware:
    """Pure ASGI, so the shop is set for the whole request - including the
    streamed chat reply, which runs after a normal middleware has returned."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        # The install page and Shopify's webhooks name shops that are not saved
        # yet (that is what they are for) and check Shopify's own signature.
        if scope.get("path", "").startswith(_OWN_CHECKS):
            return await self.app(scope, receive, send)
        asked = normalise(_requested(scope))
        if asked and (not _DOMAIN_RE.match(asked) or asked not in allowed()):
            body = json.dumps({"detail": "This shop is not set up for the assistant."}).encode()
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"access-control-allow-origin", b"*")]})
            await send({"type": "http.response.body", "body": body})
            return
        token = _current.set(asked or None)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)
