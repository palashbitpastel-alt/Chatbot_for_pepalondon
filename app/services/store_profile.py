"""What the shop is, in the words a shopper hears.

The agent is told nothing about the store it works for unless something passes
it along. A hello and "what do you sell?" need the same answer - the name, what
we sell, the main categories - so it is built once, here.
"""

import logging

from app.core.config import settings
from app.services import shopify_storefront
from app.services.suggestions import plural

logger = logging.getLogger(__name__)

OVERVIEW_CATEGORIES = 8     # read this many, then fold "Dress"/"Dresses" together


def _singular(name: str) -> str:
    key = name.strip().lower()
    if key.endswith("es") and key[:-2].endswith("ss"):
        return key[:-2]
    if key.endswith("s") and not key.endswith("ss"):
        return key[:-1]
    return key


async def store_name() -> str:
    """What the shop is called: the configured brand, else Shopify's own name."""
    configured = settings.SUPPORT_STORE_NAME.strip()
    if configured:
        return configured
    return (await shopify_storefront.shop_info())["name"]


async def main_categories(limit: int = 6) -> list[str]:
    """The fullest categories, already plural, with "Dress"/"Dresses" twins folded
    together - they are separate product types but one shelf to a shopper."""
    listed = (await shopify_storefront.categories(OVERVIEW_CATEGORIES))["categories"]
    names: dict[str, str] = {}
    for entry in listed:
        names.setdefault(_singular(entry["name"]), plural(entry["name"]))
    return list(names.values())[:limit]


async def overview(limit: int = 6) -> dict:
    """Name, what we sell and the main categories.

    Deliberately no product count: a raw "50 products" makes a curated range
    sound small, and it is out of date the moment the catalogue changes. Each
    part is optional, so a slow lookup costs a detail rather than the answer.
    """
    about: dict = {"what_we_sell": settings.SUPPORT_STORE_DESCRIPTION}
    try:
        about["name"] = await store_name()
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the store name", exc_info=True)
    try:
        about["categories"] = await main_categories(limit)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the main categories", exc_info=True)
    return about
