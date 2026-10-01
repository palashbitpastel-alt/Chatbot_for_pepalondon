"""Which of the store's own colour names a shopper means by a colour word.

"She doesn't like pink" must also rule out "Dusty Raspberry" and "Baby Pink",
yet no list of shades is written here - every store names its colours its own
way. The model reads every option value the store uses and says which basic
colours a shopper might call each one ("Dusty Raspberry" -> pink, red). That is
read once, in the background, and cached; a turn never waits long for it, and
until it is ready the colour word itself is matched, as before.

Asking "which of these are pink?" each time gave different answers from one
call to the next; sorting every name into families once is steadier, and
leans towards including a name a shopper might see as that colour.
"""

import asyncio
import json
import logging
import re
import time

logger = logging.getLogger(__name__)

CACHE_SECONDS = 6 * 60 * 60
BATCH = 40
TIMEOUT_SECONDS = 45
WAIT_IN_TURN_SECONDS = 8

# lower-cased option value -> the basic colours it reads as
_families: dict[str, set[str]] = {}
_read_at = 0.0
_task: asyncio.Task | None = None

_PROMPT = """You read a children's clothing shop's product option values. Some are colour names
(often fancy: "Dusty Raspberry", "Racing Green", "Oatmeal"), the rest are sizes or other options.
For EVERY value that names a colour, list the basic colour words a shopper might call it -
include each one a shopper could reasonably see in it ("Dusty Raspberry": ["pink", "red"],
"Teal": ["green", "blue"]). Use plain basic colour words: red, pink, orange, yellow, green, blue,
purple, brown, beige, cream, black, white, grey, gold, silver, multicolour.
Return ONLY a JSON object mapping each colour value, exactly as written, to its list.
Leave out values that are not colours."""


def _word(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


async def _read_batch(values: list[str]) -> dict[str, set[str]]:
    from app.agent.base import build_llm
    answer = await asyncio.wait_for(build_llm(temperature=0, max_tokens=3000).ainvoke([
        ("system", _PROMPT), ("human", json.dumps(values, ensure_ascii=False))]), timeout=TIMEOUT_SECONDS)
    text = answer.content if isinstance(answer.content, str) else str(answer.content)
    read = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    known = {_word(v) for v in values}
    return {_word(k): {_word(c) for c in v if isinstance(c, str)}
            for k, v in read.items() if _word(k) in known and isinstance(v, list)}


async def _learn() -> None:
    global _families, _read_at
    from app.services import outfit
    catalogue = await outfit.browse_catalogue()
    values = sorted({v for p in catalogue.get("products") or [] for v in p.get("option_values") or []})
    # A size always carries a number ("5Y", "28EU", "3-6M"); a colour name does not.
    values = [v for v in values if not re.search(r"\d", v)]
    if not values:
        return
    batches = [values[i:i + BATCH] for i in range(0, len(values), BATCH)]
    found: dict[str, set[str]] = {}
    for result in await asyncio.gather(*(_read_batch(b) for b in batches), return_exceptions=True):
        if isinstance(result, Exception):
            logger.warning("Could not read some of the store's colour names: %s", result)
            continue
        found.update(result)
    if found:
        _families = found
        _read_at = time.monotonic()
        logger.info("Read %d store colour names into colour families", len(found))


def warm() -> asyncio.Task | None:
    """Start reading the store's colour names unless a fresh reading exists."""
    global _task
    if _families and time.monotonic() - _read_at < CACHE_SECONDS:
        return None
    if _task is None or _task.done():
        _task = asyncio.create_task(_learn())
    return _task


async def shades_of(colours: tuple[str, ...]) -> tuple[str, ...]:
    """The colours themselves plus the store's colour names that read as them."""
    wanted = {_word(c) for c in colours if c}
    if not wanted:
        return ()
    task = warm()
    if task is not None and not _families:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=WAIT_IN_TURN_SECONDS)
        except Exception:  # noqa: BLE001 - still reading: the word alone for now
            pass
    shades = {name for name, fams in _families.items() if fams & wanted}
    return tuple(sorted(wanted | shades))
