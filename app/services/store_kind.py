"""What kind of shop this is, read from the shop itself.

The app is installed on many stores, so no prompt may say what a store sells.
Each prompt that needs it says "{shop_kind}" instead, and fill() puts in this
shop's own reading: the model reads the shop's name, its own description and a
sample of its products once, and says in a few words what kind of shop it is
("a children's clothing shop", "a shop selling camping gear"), whether it
sells clothes for children, and how its sizes are written.

The reading is kept per shop in the database and read again in the background
once it is older than STORE_KIND_REFRESH_HOURS, so a shop that changes what it
sells is read afresh. Until a shop has been read - or if reading fails - the
prompts say NEUTRAL, which assumes nothing.
"""

import asyncio
import json
import logging
import re
import time

from app.core.config import settings
from app.services import shops

logger = logging.getLogger(__name__)

KEY = "store_kind"
NEUTRAL = "an online shop"
PLACEHOLDER = "{shop_kind}"
PLACEHOLDER_CAPITAL = "{Shop_kind}"
# How many products the reader sees: enough to tell what the shop sells.
SAMPLE = 80
TIMEOUT_SECONDS = 25
RETRY_SECONDS = 600

_ASK = (
    "Here is an online shop: its name, its own description (if it wrote one) and "
    "a sample of its products (title | category | sizes).\n\n{shop}\n\n"
    "Say what kind of shop it is, judged only from this. Answer with JSON only:\n"
    '{{"kind": "<a short noun phrase starting with a or an, e.g. a shop selling '
    'camping gear, a jewellery shop, a children\'s clothing shop>", '
    '"childrenswear": <true if it mainly sells clothes or shoes for babies and children>, '
    '"sizes": "<one short line on how its sizes are written, or none>"}}'
)

_known_BY_SHOP: dict = {}    # per shop: the reading, once loaded
_loading_BY_SHOP: dict = {}  # per shop: a reading under way
_failed_BY_SHOP: dict = {}   # per shop: when the last reading failed


async def _load() -> dict | None:
    try:
        from sqlalchemy import select

        from app.db.models import StoreSetting
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(StoreSetting).where(
                StoreSetting.key == shops.key(KEY)))).scalar_one_or_none()
        return row.value if row and isinstance(row.value, dict) else None
    except Exception:  # noqa: BLE001
        logger.warning("Could not load the store reading", exc_info=True)
        return None


async def _save(value: dict) -> None:
    try:
        from sqlalchemy import select

        from app.db.models import StoreSetting
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(StoreSetting).where(
                StoreSetting.key == shops.key(KEY)))).scalar_one_or_none()
            if row is None:
                db.add(StoreSetting(key=shops.key(KEY), value=value))
            else:
                row.value = value
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Could not keep the store reading", exc_info=True)


async def _describe_shop() -> str:
    from app.services import outfit, store_profile

    lines = []
    try:
        lines.append(f"Name: {await store_profile.store_name()}")
    except Exception:  # noqa: BLE001
        pass
    description = shops.setting("description", settings.SUPPORT_STORE_DESCRIPTION)
    if description:
        lines.append(f"Its description: {description}")
    catalogue = await outfit.browse_catalogue()
    for p in (catalogue.get("products") or [])[:SAMPLE]:
        sizes = ", ".join(str(s) for s in (p.get("sizes") or [])[:6])
        lines.append(f"- {p.get('title')} | {p.get('category') or ''} | {sizes}")
    return "\n".join(lines)


async def learn() -> dict | None:
    """Read this shop now and keep the reading. None when it could not be read."""
    from app.agent.base import build_llm

    try:
        about = await _describe_shop()
        answer = await asyncio.wait_for(build_llm(temperature=0, max_tokens=300).ainvoke(
            _ASK.format(shop=about),
            # Inside a shopper's turn: its own callbacks, or the JSON would
            # stream to them ahead of their answer.
            config={"callbacks": [], "tags": ["store_kind"], "run_name": "learn_store_kind"}),
            TIMEOUT_SECONDS)
        text = answer.content if hasattr(answer, "content") else str(answer)
        found = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        kind = str(found.get("kind") or "").strip().rstrip(".")
        if not kind:
            raise ValueError("no kind")
        value = {"kind": kind[:80], "childrenswear": bool(found.get("childrenswear")),
                 "sizes": str(found.get("sizes") or "").strip()[:160], "read_at": time.time()}
    except Exception:  # noqa: BLE001 - prompts fall back to NEUTRAL
        logger.warning("Could not read what kind of shop %s is", shops.current(), exc_info=True)
        return None
    shops.scoped(_known_BY_SHOP, dict).update(value)
    await _save(value)
    logger.info("Shop %s read as %s", shops.current(), value["kind"])
    return value


async def _refresh() -> None:
    try:
        await learn()
    finally:
        _loading_BY_SHOP.pop(shops.current(), None)


async def profile() -> dict:
    """This shop's reading: loaded once per process, read if it never was, and
    read again in the background once it is old. Empty when none exists yet.

    While a reading is under way - reading the catalogue asks other readers,
    which ask for this - the answer is empty rather than a second reading. A
    reading that failed is tried again only after RETRY_SECONDS."""
    shop = shops.current()
    known = shops.scoped(_known_BY_SHOP, dict)
    if _loading_BY_SHOP.get(shop):
        return dict(known)
    if not known:
        kept = await _load()
        if kept:
            known.update(kept)
        elif time.time() - _failed_BY_SHOP.get(shop, 0.0) > RETRY_SECONDS:
            _loading_BY_SHOP[shop] = True
            try:
                if await learn() is None:
                    _failed_BY_SHOP[shop] = time.time()
            finally:
                _loading_BY_SHOP.pop(shop, None)
    stale = time.time() - float(known.get("read_at") or 0) > settings.STORE_KIND_REFRESH_HOURS * 3600
    if known and stale and not _loading_BY_SHOP.get(shop):
        _loading_BY_SHOP[shop] = True
        asyncio.create_task(_refresh())
    return dict(known)


_NOT_CHILDRENSWEAR = (
    "[This shop is {kind}. Where your instructions speak of a child, a baby, an "
    "age or age sizes, read them as the person the shopper is buying for and this "
    "shop's own sizes{sizes}. Never ask a child's age or whether it is for a boy "
    "or a girl unless the shopper brings it up.]"
)


def instructions_note(reading: dict) -> str | None:
    """For a shop read as anything but children's clothing, how to read the
    agent's instructions; None for children's clothing or an unread shop, so
    those turns are exactly as before."""
    if not reading or reading.get("childrenswear") or not reading.get("kind"):
        return None
    sizes = f" ({reading['sizes']})" if reading.get("sizes") and reading["sizes"].lower() != "none" else ""
    return _NOT_CHILDRENSWEAR.format(kind=reading["kind"], sizes=sizes)


async def phrase() -> str:
    """"a children's clothing shop" - or NEUTRAL until the shop has been read."""
    return (await profile()).get("kind") or NEUTRAL


async def fill(text: str) -> str:
    """A prompt with {shop_kind} (and {Shop_kind}, capitalised) put in."""
    if PLACEHOLDER not in text and PLACEHOLDER_CAPITAL not in text:
        return text
    kind = await phrase()
    return text.replace(PLACEHOLDER, kind).replace(PLACEHOLDER_CAPITAL, kind[:1].upper() + kind[1:])
