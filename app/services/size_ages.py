"""Which child each size fits, for pieces whose sizes are not written as ages.

Clothes say their age on the label ("10Y", "18M"). Shoes, belts, hats and
one-size pieces do not: "1UK/2US/33EU", "L / 70cm", "S-L", "One Size". Asked
for "everything in 10Y", only the age labels matched, so a ten year old's
plimsolls, boots and belts never came back although the shop sells them in his
size.

Nothing here knows what those labels mean. The model reads each piece - its
name, type, tags and its own sizes, in whatever form the shop wrote them - once,
and says for each size the ages of the child it fits, or that it fits no child
(a toy, an adult's belt). The reading is kept in the database like the other
product readings (services/suits) and done again only when the piece changes.
"""

import asyncio
import hashlib
import json
import logging
import re

from app.services import shops

logger = logging.getLogger(__name__)

BATCH = 15
READINGS_KEY = "size_age_readings"
# Part of every reading's fingerprint: raised when the way pieces are read
# changes, so the readings kept from before are read again.
READ_VERSION = 2
# A shopper's turn never waits longer than this for a first reading; what was
# not read in time is simply left out, and read on a later turn.
TIMEOUT_SECONDS = 25

_ASK = (
    "You are reading a children's clothing shop's own product list.\n"
    "These pieces are not sized by age: their sizes are written the way the "
    "shop sells them - shoe sizes, measurements, letters, one size, or anything else.\n"
    "Each piece's sizes are numbered. For each piece and each numbered size, judge "
    "from the piece itself and your knowledge of children's sizing which ages of "
    "child (in years, decimals for babies) that size fits. Give null for a size no "
    "child wears, and for every size of a piece that is not something a child wears "
    "in a size (a toy, a gift card, an adult's item).\n\n"
    "{pieces}\n\n"
    'Answer with JSON only, keyed by handle and by size number: '
    '{{"<handle>": {{"1": [youngest, oldest] or null, "2": ...}}, ...}}'
)

_known_BY_SHOP: dict = {}   # per shop: see services/shops
_loaded_BY_SHOP: dict = {}


def _sizes(piece: dict) -> list[str]:
    return [str(s) for s in piece.get("sizes") or [] if str(s).strip()]


def _source(piece: dict) -> str:
    parts = [piece.get("title") or ""]
    for label, key in (("type", "product_type"), ("category", "category"), ("tags", "tags")):
        if piece.get(key):
            parts.append(f"{label}: {piece[key]}")
    parts.append("sizes: " + json.dumps({str(n): s for n, s in enumerate(_sizes(piece), 1)}, ensure_ascii=False))
    return " | ".join(parts)


def _fingerprint(piece: dict) -> str:
    return hashlib.sha1(f"{READ_VERSION}|{_source(piece)}".encode("utf-8")).hexdigest()[:12]


async def _load() -> None:
    if _loaded_BY_SHOP.get(shops.current(), False):
        return
    _loaded_BY_SHOP[shops.current()] = True
    try:
        from sqlalchemy import select

        from app.db.models import StoreSetting
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(StoreSetting).where(
                StoreSetting.key == shops.key(READINGS_KEY)))).scalar_one_or_none()
        if row and isinstance(row.value, dict):
            shops.scoped(_known_BY_SHOP, dict).update({h: r for h, r in row.value.items() if isinstance(r, dict)})
    except Exception:  # noqa: BLE001 - the readings are a saving, never a requirement
        logger.warning("Could not load the kept size readings", exc_info=True)


async def _save() -> None:
    try:
        from sqlalchemy import select

        from app.db.models import StoreSetting
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(StoreSetting).where(
                StoreSetting.key == shops.key(READINGS_KEY)))).scalar_one_or_none()
            if row is None:
                db.add(StoreSetting(key=shops.key(READINGS_KEY), value=dict(shops.scoped(_known_BY_SHOP, dict))))
            else:
                row.value = dict(shops.scoped(_known_BY_SHOP, dict))
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Could not keep the size readings", exc_info=True)


def _ages(raw) -> list[float] | None:
    if isinstance(raw, str):
        raw = re.findall(r"\d+(?:\.\d+)?", raw)[:2]
    if isinstance(raw, (int, float)):
        raw = [raw, raw]
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        low, high = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None
    return [min(low, high), max(low, high)] if high >= 0 else None


async def _learn(pieces: list[dict]) -> None:
    await _load()
    known = shops.scoped(_known_BY_SHOP, dict)
    unread = [p for p in pieces if p.get("handle") and _sizes(p)
              and (known.get(p["handle"]) or {}).get("from") != _fingerprint(p)]
    if not unread:
        return
    from app.agent.base import build_llm

    model = build_llm(temperature=0, max_tokens=2500)
    gate = asyncio.Semaphore(3)

    async def read_batch(chunk: list[dict]) -> bool:
        lines = "\n".join(f'- {p["handle"]}: {_source(p)}' for p in chunk)
        try:
            async with gate:
                answer = await model.ainvoke(
                    _ASK.format(pieces=lines),
                    # Inside a shopper's turn: its own callbacks, or the JSON
                    # would stream to them ahead of their answer.
                    config={"callbacks": [], "tags": ["size_ages"], "run_name": "learn_size_ages"})
            text = answer.content if hasattr(answer, "content") else str(answer)
            block = re.search(r"\{.*\}", text, re.S)
            read = json.loads(block.group(0)) if block else {}
        except Exception:  # noqa: BLE001 - never fail a shopper's turn over this
            logger.warning("Could not read the sizes of %d pieces", len(chunk), exc_info=True)
            return False
        got = False
        for piece in chunk:
            found = read.get(piece["handle"])
            if not isinstance(found, dict):
                continue
            known[piece["handle"]] = {
                "sizes": {s: _ages(found.get(str(n), found.get(s))) for n, s in enumerate(_sizes(piece), 1)},
                "from": _fingerprint(piece)}
            got = True
        return got

    done = await asyncio.gather(*(read_batch(unread[i:i + BATCH]) for i in range(0, len(unread), BATCH)))
    if any(done):
        await _save()


async def learn(pieces: list[dict]) -> None:
    """Read the pieces not read yet, or changed since - within TIMEOUT_SECONDS."""
    try:
        await asyncio.wait_for(_learn(pieces), TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - asyncio.TimeoutError included
        logger.warning("Size readings not finished in time; the rest are read next turn", exc_info=True)


def sizes_for_ages(piece: dict, low: float, high: float) -> list[str]:
    """The piece's own sizes that fit a child of these ages, as the model read them."""
    read = (shops.scoped(_known_BY_SHOP, dict).get(piece.get("handle") or "") or {})
    if read.get("from") != _fingerprint(piece):
        return []
    return [s for s, span in (read.get("sizes") or {}).items()
            if span and span[0] <= high and low <= span[1]]
