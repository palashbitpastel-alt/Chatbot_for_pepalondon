"""Who a product is for - Boys, Girls or Baby - read by the model, not from a list.

Every store tags differently: "Boys", "boy", "Kids-Boys", "Garçon", "Baby Girl",
or no audience tag at all. Hardcoding "Boys"/"Girls"/"Baby" worked for one test
store and would have hidden half the catalogue of the next. So:

1. The store's own tag list is read once and the model says which tags mean
   which child (`ensure`).
2. Products with no such tag are read by title and product type, in batches,
   and the model says who each is for (`learn`).

Both are cached for a few hours. "Boys", "Girls" and "Baby" remain only as the
three groups the assistant thinks in; they are no longer the store's tag names.
If the model cannot be reached, exact tag names are used as before.
"""

import asyncio
import json
import logging
import re
import time

from app.services.shopify_client import ShopifyError, graphql

logger = logging.getLogger(__name__)

GROUPS = ("Boys", "Girls", "Baby")
CACHE_SECONDS = 6 * 60 * 60
TIMEOUT_SECONDS = 20
BATCH = 80
MAX_TAG_PAGES = 20

TAGS_QUERY = """
query AudienceTags($cursor: String) {
  productTags(first: 250, after: $cursor) {
    nodes
    pageInfo { hasNextPage endCursor }
  }
}
"""

# group -> the store's own tags (lower-cased) that mean it.
_tag_map: tuple[float, dict[str, set[str]]] | None = None
# product id -> groups the model read off its name ([] means anyone).
_products: dict[str, tuple[float, list[str]]] = {}
_lock = asyncio.Lock()

_TAG_PROMPT = """You map a children's clothing shop's product tags to who a product is for.
Groups: "Boys", "Girls", "Baby" (a baby of either sex).
Return ONLY a JSON object {"Boys": [...], "Girls": [...], "Baby": [...]} listing, exactly as
written, the tags that say a product is for that group - in any language or spelling
("boy", "Kids-Boys", "Garçon", "Baby Girl" belongs to Girls AND Baby). A tag that is a
colour, size, fabric, occasion, product kind or anything else goes nowhere. Adult tags
("men", "women") go nowhere."""

_PRODUCT_PROMPT = """You read a children's clothing shop's products and say who each is for.
Groups: "Boys", "Girls", "Baby". Return ONLY a JSON object mapping each id to a list of
groups. Use [] when the product suits any child (most basics, shoes and accessories do)
or you cannot tell. Judge from what the product is, not from colour or pattern alone:
a floral shirt can be a boy's; a dress, skirt or blouse is a girl's; a romper, bodysuit
or babygrow for 0-24 months is Baby."""


def _fallback() -> dict[str, set[str]]:
    return {g: {g.lower()} for g in GROUPS}


async def _llm_json(system: str, human: str) -> dict:
    from app.agent.base import build_llm
    llm = build_llm(temperature=0, max_tokens=4000)
    answer = await asyncio.wait_for(llm.ainvoke([("system", system), ("human", human)]),
                                    timeout=TIMEOUT_SECONDS)
    text = answer.content if isinstance(answer.content, str) else str(answer.content)
    found = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    if not isinstance(found, dict):
        raise ValueError("not an object")
    return found


async def _store_tags() -> list[str]:
    tags: list[str] = []
    cursor = None
    for _ in range(MAX_TAG_PAGES):
        page = (await graphql(TAGS_QUERY, {"cursor": cursor}))["productTags"]
        tags += [t for t in page.get("nodes") or [] if isinstance(t, str)]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return tags


async def ensure() -> None:
    """Read which of the store's tags mean Boys, Girls or Baby (cached)."""
    global _tag_map
    if _tag_map and time.monotonic() - _tag_map[0] < CACHE_SECONDS:
        return
    async with _lock:
        if _tag_map and time.monotonic() - _tag_map[0] < CACHE_SECONDS:
            return
        mapping = _fallback()
        try:
            tags = await _store_tags()
            if tags:
                read = await _llm_json(_TAG_PROMPT, json.dumps(tags, ensure_ascii=False))
                known = {t.lower(): t for t in tags}
                mapping = {g: {t.lower() for t in (read.get(g) or []) if isinstance(t, str) and t.lower() in known}
                           for g in GROUPS}
                # The model must not lose a tag that literally is the group name.
                for g in GROUPS:
                    if g.lower() in known:
                        mapping[g].add(g.lower())
        except (ShopifyError, KeyError, ValueError, AttributeError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            logger.warning("Could not read the store's audience tags; using exact names", exc_info=True)
            # Keep an older reading rather than fall back to bare names.
            if _tag_map:
                mapping = _tag_map[1]
        _tag_map = (time.monotonic(), mapping)
        logger.info("Audience tags: %s", {g: sorted(v) for g, v in mapping.items()})


def tags_for(group: str) -> list[str]:
    """The store's tags that mean this group (for a Shopify tag search)."""
    mapping = _tag_map[1] if _tag_map else _fallback()
    return sorted(mapping.get(group) or {group.lower()})


def of(tags: list[str] | None, product_id: str | int | None = None) -> list[str]:
    """Who a product is for: by the store's tags, else by what the model read
    off its name. [] means any child."""
    mapping = _tag_map[1] if _tag_map else _fallback()
    lowered = {str(t).strip().lower() for t in tags or []}
    found = [g for g in GROUPS if lowered & mapping.get(g, set())]
    if found:
        return found
    hit = _products.get(str(product_id)) if product_id is not None else None
    return list(hit[1]) if hit else []


async def learn(products: list[dict]) -> None:
    """Have the model read who each untagged product is for (cached per product).

    `products` carry product_id, title, category and their tags (or "for")."""
    now = time.monotonic()
    todo = []
    for p in products:
        pid = str(p.get("product_id") or "")
        if not pid or of(p.get("tags"), None):
            continue
        cached = _products.get(pid)
        if cached and now - cached[0] < CACHE_SECONDS:
            continue
        todo.append(p)
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        listing = {str(p["product_id"]): f'{p.get("title") or ""} | {p.get("category") or ""}' for p in chunk}
        try:
            read = await _llm_json(_PRODUCT_PROMPT, json.dumps(listing, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - unread products simply suit any child
            logger.warning("Could not read who %d products are for", len(chunk), exc_info=True)
            continue
        for pid in listing:
            groups = [g for g in (read.get(pid) or []) if g in GROUPS]
            _products[pid] = (now, groups)
