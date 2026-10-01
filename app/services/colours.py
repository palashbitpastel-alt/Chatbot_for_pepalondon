"""Which of the store's own colour names a shopper means by a colour word.

"She doesn't like pink" must also rule out "Dusty Raspberry" and "Baby Pink",
yet no list of shades is written here - every store names its colours its own
way. The model reads the store's option values once per colour and says which
of them a shopper would see as that colour. Cached; on failure only the word
itself is used, as before.
"""

import asyncio
import json
import logging
import re
import time

logger = logging.getLogger(__name__)

CACHE_SECONDS = 6 * 60 * 60
TIMEOUT_SECONDS = 15

_cache: dict[str, tuple[float, tuple[str, ...]]] = {}

_PROMPT = """A shopper at a children's clothing shop said they do NOT want this colour: {colour}.
Below are the shop's own product option values (colour names, sizes and other options mixed).
Return ONLY a JSON object {{"matches": [...]}} listing, exactly as written, every value that
names a colour a shopper would see as {colour} (its shades and named tones count - a shade
of it, or a fabric/print whose main colour is it). Leave out sizes, other colours and
anything you are unsure of."""


async def shades_of(colours: tuple[str, ...]) -> tuple[str, ...]:
    """The colours themselves plus the store's names for their shades (lower-case)."""
    if not colours:
        return ()
    found: set[str] = set(colours)
    for colour in colours:
        hit = _cache.get(colour)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            found.update(hit[1])
            continue
        try:
            from app.agent.base import build_llm
            from app.services import outfit
            catalogue = await outfit.browse_catalogue()
            values = sorted({v for p in catalogue.get("products") or [] for v in p.get("option_values") or []})
            if not values:
                continue
            answer = await asyncio.wait_for(build_llm(temperature=0, max_tokens=800).ainvoke([
                ("system", _PROMPT.format(colour=colour)),
                ("human", json.dumps(values, ensure_ascii=False)),
            ]), timeout=TIMEOUT_SECONDS)
            text = answer.content if isinstance(answer.content, str) else str(answer.content)
            read = json.loads(re.search(r"\{.*\}", text, re.S).group(0)).get("matches") or []
            known = {v.lower(): v for v in values}
            shades = tuple(sorted({m.lower() for m in read if isinstance(m, str) and m.lower() in known}))
            _cache[colour] = (time.monotonic(), shades)
            logger.info("Shades of %s in this store: %s", colour, shades)
            found.update(shades)
        except Exception:  # noqa: BLE001 - the word alone still works
            logger.warning("Could not read the store's shades of %s", colour, exc_info=True)
    return tuple(sorted(found))
