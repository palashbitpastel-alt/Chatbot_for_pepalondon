"""Shared plumbing for the Shopify Admin GraphQL API.

Both the background sync and the customer support agent's tools talk to Shopify
through here, so the endpoint, auth header, error handling and timeout live in
one place. Callers pass a named operation and variables — never an
agent-composed query string, so a shopper can never steer what is asked for.
"""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

TIMEOUT = 30.0


class ShopifyError(RuntimeError):
    """A Shopify call failed. The message is safe to surface to an agent."""


def is_configured() -> bool:
    return bool(settings.SHOPIFY_STORE_URL and settings.SHOPIFY_ACCESS_TOKEN)


def store_domain() -> str:
    """The bare myshopify host, e.g. "palashstor.myshopify.com"."""
    return settings.SHOPIFY_STORE_URL.removeprefix("https://").removeprefix("http://").strip("/")


def graphql_url() -> str:
    return f"https://{store_domain()}/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json"


_scopes: set[str] | None = None


async def granted_scopes() -> set[str]:
    """The scopes this access token really holds.

    Cached for the process: they change only when the app is reinstalled, and
    every write path asks before it starts.
    """
    global _scopes
    if _scopes is None:
        data = await graphql("{ currentAppInstallation { accessScopes { handle } } }")
        _scopes = {s["handle"] for s in data["currentAppInstallation"]["accessScopes"]}
    return _scopes


async def can(scope: str) -> bool:
    """Whether the token holds ``scope``. False if we cannot find out.

    Failing closed is deliberate: the caller uses this to decide whether to
    promise a shopper a write, and an unanswerable question is not a yes.
    """
    try:
        return scope in await granted_scopes()
    except ShopifyError:
        logger.warning("Could not read the token's scopes; assuming %s is absent", scope)
        return False


async def graphql(query: str, variables: dict | None = None, client: httpx.AsyncClient | None = None) -> dict:
    """Run one GraphQL operation and return its ``data``.

    Raises ShopifyError on transport failure or a GraphQL ``errors`` payload,
    including the partial-access case where one field is denied for a missing
    scope but the rest of the response is fine.
    """
    if not is_configured():
        raise ShopifyError("Shopify is not configured (missing store URL or access token)")

    async def _post(c: httpx.AsyncClient) -> dict:
        response = await c.post(
            graphql_url(),
            json={"query": query, "variables": variables or {}},
            headers={"X-Shopify-Access-Token": settings.SHOPIFY_ACCESS_TOKEN},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    try:
        payload = await _post(client) if client is not None else await _post_with_new_client(_post)
    except httpx.HTTPError as exc:
        raise ShopifyError(f"Could not reach Shopify: {exc}") from exc

    if payload.get("errors"):
        messages = "; ".join(e.get("message", "unknown") for e in payload["errors"])
        raise ShopifyError(f"Shopify rejected the request: {messages}")
    return payload["data"]


async def _post_with_new_client(post) -> dict:
    async with httpx.AsyncClient() as c:
        return await post(c)
