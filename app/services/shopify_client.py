"""Shared plumbing for the Shopify Admin GraphQL API.

Both the background sync and the customer support agent's tools talk to Shopify
through here, so the endpoint, auth header, error handling and timeout live in
one place. Callers pass a named operation and variables — never an
agent-composed query string, so a shopper can never steer what is asked for.
"""

import asyncio
import logging
import time

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

TIMEOUT = 30.0


class ShopifyError(RuntimeError):
    """A Shopify call failed. The message is safe to surface to an agent."""


def is_configured() -> bool:
    has_credentials = settings.SHOPIFY_ACCESS_TOKEN or (
        settings.SHOPIFY_CLIENT_ID and settings.SHOPIFY_CLIENT_SECRET
    )
    return bool(settings.SHOPIFY_STORE_URL and has_credentials)


def store_domain() -> str:
    """The bare myshopify host, e.g. "palashstor.myshopify.com"."""
    return settings.SHOPIFY_STORE_URL.removeprefix("https://").removeprefix("http://").strip("/")


def graphql_url() -> str:
    return f"https://{store_domain()}/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json"


_token: str | None = None
_token_expires_at = 0.0
_token_lock = asyncio.Lock()


async def access_token(force_refresh: bool = False) -> str:
    """The Admin API token to send.

    A fixed SHOPIFY_ACCESS_TOKEN wins when set. Otherwise the token comes from
    the client credentials grant, which Dev Dashboard apps use and which only
    lives 24 hours, so it is fetched on demand and renewed an hour early.
    """
    global _token, _token_expires_at
    if settings.SHOPIFY_ACCESS_TOKEN:
        return settings.SHOPIFY_ACCESS_TOKEN
    async with _token_lock:
        if _token and not force_refresh and time.monotonic() < _token_expires_at:
            return _token
        try:
            async with httpx.AsyncClient() as c:
                response = await c.post(
                    f"https://{store_domain()}/admin/oauth/access_token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": settings.SHOPIFY_CLIENT_ID,
                        "client_secret": settings.SHOPIFY_CLIENT_SECRET,
                    },
                    timeout=TIMEOUT,
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise ShopifyError(f"Could not get a Shopify access token: {exc}") from exc
        _token = body["access_token"]
        _token_expires_at = time.monotonic() + max(int(body.get("expires_in", 86399)) - 3600, 60)
        return _token


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
        async def send(token: str) -> httpx.Response:
            return await c.post(
                graphql_url(),
                json={"query": query, "variables": variables or {}},
                headers={"X-Shopify-Access-Token": token},
                timeout=TIMEOUT,
            )

        response = await send(await access_token())
        if response.status_code == 401 and not settings.SHOPIFY_ACCESS_TOKEN:
            # Revoked or rotated early: fetch a fresh one and try once more.
            response = await send(await access_token(force_refresh=True))
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
