"""Shopify's own storefront search, for finding products the way the shop's
search panel does.

The Admin API's product filter matches the words as written. The storefront
search ranks by relevance across title, type, tags, description and variants,
and applies the synonyms the merchant set up in Search & Discovery - the
shop's own data, not a list kept here.

It needs a Storefront API token: SHOPIFY_STOREFRONT_TOKEN if one is set,
otherwise one the app creates for itself through the Admin API (kept in the
database), when its scopes allow. With neither, ``product_ids`` returns None
and callers keep using the Admin search.
"""

import logging
import time

import httpx

from app.core.config import settings
from app.services.shopify_client import ShopifyError, graphql, store_domain
from app.services import shops

logger = logging.getLogger(__name__)

TOKEN_KEY = "storefront_search_token"
# After a failed attempt to get a token, wait this long before trying again.
RETRY_SECONDS = 3600

SEARCH = """
query Find($q: String!, $first: Int!) {
  search(query: $q, first: $first, types: [PRODUCT], unavailableProducts: LAST) {
    nodes { ... on Product { id } }
  }
}
"""

CREATE_TOKEN = """
mutation MakeToken($input: StorefrontAccessTokenInput!) {
  storefrontAccessTokenCreate(input: $input) {
    storefrontAccessToken { accessToken }
    userErrors { message }
  }
}
"""

_token_BY_SHOP: dict = {}  # per shop: see services/shops
_failed_at_BY_SHOP: dict = {}  # per shop: see services/shops


async def _kept(value: str | None = None) -> str | None:
    """Read the token kept in the database, or keep a new one."""
    try:
        from sqlalchemy import select

        from app.db.models import StoreSetting
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(StoreSetting).where(StoreSetting.key == shops.key(TOKEN_KEY)))).scalar_one_or_none()
            if value is None:
                return (row.value or {}).get("token") if row and isinstance(row.value, dict) else None
            if row is None:
                db.add(StoreSetting(key=shops.key(TOKEN_KEY), value={"token": value}))
            else:
                row.value = {"token": value}
            await db.commit()
    except Exception:  # noqa: BLE001 - a kept token is a saving, never a requirement
        logger.warning("Could not use the kept storefront token", exc_info=True)
    return value


async def _storefront_token() -> str | None:
    if settings.SHOPIFY_STOREFRONT_TOKEN:
        return settings.SHOPIFY_STOREFRONT_TOKEN
    if _token_BY_SHOP.get(shops.current(), None):
        return _token_BY_SHOP.get(shops.current(), None)
    if time.monotonic() - _failed_at_BY_SHOP.get(shops.current(), 0.0) < RETRY_SECONDS and _failed_at_BY_SHOP.get(shops.current(), 0.0):
        return None
    _token_BY_SHOP[shops.current()] = await _kept()
    if _token_BY_SHOP.get(shops.current(), None):
        return _token_BY_SHOP.get(shops.current(), None)
    try:
        made = (await graphql(CREATE_TOKEN, {"input": {"title": "Pepa Assistant search"}}))["storefrontAccessTokenCreate"]
        token = (made.get("storefrontAccessToken") or {}).get("accessToken")
        if not token:
            raise ShopifyError("; ".join(e.get("message", "") for e in made.get("userErrors") or []) or "no token")
    except (ShopifyError, KeyError, TypeError) as exc:
        _failed_at_BY_SHOP[shops.current()] = time.monotonic()
        logger.warning("Storefront search is off - no Storefront API token: %s", exc)
        return None
    _token_BY_SHOP[shops.current()] = await _kept(token)
    return _token_BY_SHOP.get(shops.current(), None)


async def product_ids(query: str, first: int) -> list[str] | None:
    """Product ids for a search, most relevant first. None when the storefront
    search cannot be used, so the caller falls back to the Admin search."""
    token = await _storefront_token()
    if not token or not query.strip():
        return None
    url = f"https://{store_domain()}/api/{settings.SHOPIFY_API_VERSION}/graphql.json"
    try:
        async with httpx.AsyncClient() as c:
            response = await c.post(url, json={"query": SEARCH, "variables": {"q": query, "first": first}},
                                    headers={"X-Shopify-Storefront-Access-Token": token}, timeout=20.0)
        if response.status_code in (401, 403) and not settings.SHOPIFY_STOREFRONT_TOKEN:
            _token_BY_SHOP[shops.current()] = None                      # revoked: make a new one next time
            await _kept("")
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise ShopifyError(str(body["errors"])[:300])
        nodes = body["data"]["search"]["nodes"]
    except (httpx.HTTPError, ShopifyError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Storefront search failed for %r: %s", query, exc)
        return None
    return [n["id"].rsplit("/", 1)[-1] for n in nodes if isinstance(n, dict) and n.get("id")]
