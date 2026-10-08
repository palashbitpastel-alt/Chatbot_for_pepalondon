"""Shops that installed the app, recorded by the install itself.

When a merchant installs the app, Shopify opens the app's page in their admin
with an ``id_token``: a short JWT signed with the app's client secret, naming
the shop. We check that signature, swap the token for the shop's offline
access token (Shopify's token exchange) and save the shop. From then on the
backend serves that shop: no list to edit, whoever installed it.

Uninstalling sends the ``app/uninstalled`` webhook; the shop's token is wiped
and it stops being served. Every webhook is checked against Shopify's HMAC.

Tokens are stored encrypted with a key derived from SHOPIFY_CLIENT_SECRET, so
a copy of the database alone does not hand out shop access. Rotating the
secret makes the saved tokens unreadable: each shop then re-opens the app once
and is saved again.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.core.config import settings
from app.db.models import Shop
from app.db.session import AsyncSessionLocal
from app.services import shops

logger = logging.getLogger(__name__)

# {domain: {"token": str | None, "scopes": str, "settings": dict}} for shops
# currently installed - read once at startup and kept up to date here.
_installed: dict[str, dict] = {}


class InstallError(RuntimeError):
    """The install could not be verified or completed. Safe to show."""


def _fernet() -> Fernet:
    digest = hashlib.sha256(f"pepa-assistant:{settings.SHOPIFY_CLIENT_SECRET}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _seal(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def _open(sealed: str | None) -> str | None:
    if not sealed:
        return None
    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except InvalidToken:
        logger.warning("A saved shop token could not be read (was the client secret rotated?)")
        return None


# ── Reading what is installed ───────────────────────────────────────────────

def installed() -> set[str]:
    return {d for d, s in _installed.items() if s.get("token")}


def token_for(domain: str) -> str | None:
    return (_installed.get(shops.normalise(domain)) or {}).get("token")


def settings_for(domain: str) -> dict:
    return (_installed.get(shops.normalise(domain)) or {}).get("settings") or {}


async def load() -> None:
    """Read every installed shop into memory (at startup)."""
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Shop).where(Shop.uninstalled_at.is_(None)))).scalars().all()
        _installed.clear()
        for row in rows:
            _installed[row.domain] = {"token": _open(row.access_token), "scopes": row.scopes or "",
                                      "settings": row.settings or {}}
        logger.info("Installed shops: %s", sorted(installed()))
    except Exception:  # noqa: BLE001 - the listed shops still work without it
        logger.warning("Could not read the installed shops", exc_info=True)


# ── Install ────────────────────────────────────────────────────────────────

def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def verify_id_token(id_token: str) -> str:
    """The shop an id_token was issued for, after checking it is Shopify's."""
    try:
        head, body, sig = id_token.split(".")
        header = json.loads(_b64(head))
        claims = json.loads(_b64(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise InstallError("The install token is malformed.") from exc
    if header.get("alg") != "HS256":
        raise InstallError("The install token uses an unexpected signature.")
    expected = hmac.new(settings.SHOPIFY_CLIENT_SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64(sig)):
        raise InstallError("The install token is not signed by Shopify for this app.")
    now = time.time()
    if claims.get("aud") != settings.SHOPIFY_CLIENT_ID:
        raise InstallError("The install token is for another app.")
    if now > float(claims.get("exp", 0)) + 10 or now + 10 < float(claims.get("nbf", 0)):
        raise InstallError("The install token has expired - open the app again.")
    shop = shops.normalise(str(claims.get("dest", "")))
    if not shop.endswith(".myshopify.com"):
        raise InstallError("The install token names no shop.")
    return shop


async def exchange(shop: str, id_token: str) -> tuple[str, str]:
    """Shopify's token exchange: the id_token for the shop's offline token."""
    try:
        async with httpx.AsyncClient() as c:
            response = await c.post(
                f"https://{shop}/admin/oauth/access_token",
                json={
                    "client_id": settings.SHOPIFY_CLIENT_ID,
                    "client_secret": settings.SHOPIFY_CLIENT_SECRET,
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token": id_token,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
                    "requested_token_type": "urn:shopify:params:oauth:token-type:offline-access-token",
                },
                timeout=30,
            )
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        raise InstallError(f"Shopify did not grant access: {exc}") from exc
    return body["access_token"], body.get("scope", "")


async def save(shop: str, token: str, scopes: str) -> None:
    async with AsyncSessionLocal() as db:
        row = await db.get(Shop, shop)
        if row is None:
            row = Shop(domain=shop)
            db.add(row)
        row.access_token = _seal(token)
        row.scopes = scopes
        if row.uninstalled_at is not None:
            row.uninstalled_at = None
            row.installed_at = datetime.utcnow()
        await db.commit()
        kept = row.settings or {}
    _installed[shop] = {"token": token, "scopes": scopes, "settings": kept}
    logger.info("Saved the install of %s", shop)
    asyncio.create_task(_read_products(shop))


async def _read_products(shop: str) -> None:
    """A new shop's products, read in the background so its first chat is quick."""
    shops.set_current(shop)  # this task's own context
    try:
        from app.services import outfit, suits
        catalogue = await outfit.browse_catalogue()
        await suits.learn([p for p in catalogue["products"] if p["in_stock"]])
    except Exception:  # noqa: BLE001 - a shopper's turn reads them anyway
        logger.warning("Could not read %s's products after install", shop, exc_info=True)


async def install(id_token: str) -> str:
    """Verify, exchange and save. Returns the shop. Cheap when already saved."""
    shop = verify_id_token(id_token)
    if not token_for(shop):
        token, scopes = await exchange(shop, id_token)
        await save(shop, token, scopes)
    return shop


async def uninstall(shop: str) -> None:
    shop = shops.normalise(shop)
    async with AsyncSessionLocal() as db:
        row = await db.get(Shop, shop)
        if row is not None:
            row.access_token = None
            row.uninstalled_at = datetime.utcnow()
            await db.commit()
    _installed.pop(shop, None)
    logger.info("%s uninstalled the app", shop)


async def forget(shop: str) -> None:
    """shop/redact: everything about the shop goes, 48 hours after uninstall."""
    shop = shops.normalise(shop)
    async with AsyncSessionLocal() as db:
        row = await db.get(Shop, shop)
        if row is not None:
            await db.delete(row)
            await db.commit()
    _installed.pop(shop, None)


def webhook_is_genuine(body: bytes, signature: str | None) -> bool:
    if not signature or not settings.SHOPIFY_CLIENT_SECRET:
        return False
    digest = hmac.new(settings.SHOPIFY_CLIENT_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)
