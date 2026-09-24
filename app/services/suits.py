"""What a piece is FOR - the occasion it suits and the season it is worn in.

Both used to be keyword tables: "tartan" and "velvet" meant Christmas, "wool"
and "knit" meant winter. They are wrong in both directions. A velvet dress is
worn to a summer wedding; linen is not automatically holiday wear; and the first
piece a merchant adds whose name says neither ("The Eloise") matches nothing at
all. A shop's own words are not a controlled vocabulary, and pretending they are
puts a nightdress at the top of a list of wedding outfits.

So the piece is read rather than pattern-matched. Two things decide, in order:

  1. What the merchant has stated. A season metafield is their own answer and
     beats anybody's reading of a product name.
  2. Otherwise the model reads the name and description - once per product, in
     batches, remembered afterwards - and says which occasions it suits and
     which seasons it is worn in.

The only fixed vocabulary here is the labels, and they are ideas rather than
words: a wedding is a wedding in any shop. Nothing lists which garments count.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

OCCASIONS = ("Weddings", "Christenings", "Parties", "Christmas",
             "Occasion wear", "Holiday", "Everyday", "Sleepwear")
SEASONS = ("Spring", "Summer", "Autumn", "Winter", "All year")

# An occasion that is close enough to offer when we have nothing exact - stated
# as ideas, not as the words a product might contain.
NEIGHBOURS = {
    "Weddings": ("Christenings", "Occasion wear", "Parties"),
    "Christenings": ("Weddings", "Occasion wear"),
    "Parties": ("Occasion wear", "Christmas", "Weddings"),
    "Christmas": ("Parties", "Occasion wear"),
    "Occasion wear": ("Weddings", "Christenings", "Parties", "Christmas"),
    "Holiday": ("Everyday",),
    "Everyday": ("Holiday",),
    "Sleepwear": (),
}

BATCH = 25

_ASK = (
    "You are reading a children's clothing shop's own product list.\n"
    "For each piece, say which occasions it suits and which seasons it is worn in.\n\n"
    "Occasions, use only these: " + ", ".join(OCCASIONS) + "\n"
    "Seasons, use only these: " + ", ".join(SEASONS) + "\n\n"
    "Judge the piece itself, not the words in its name: a velvet dress suits a "
    "summer wedding as well as Christmas, and a nightdress is Sleepwear however "
    "pretty it is. Give every piece at least one occasion and one season, and "
    "list several where several are true.\n\n"
    "{pieces}\n\n"
    'Answer with JSON only: {{"<handle>": {{"occasions": [...], "seasons": [...]}}, ...}}'
)

_known: dict[str, dict] = {}


def _stated_seasons(product: dict) -> list[str]:
    """The seasons the merchant set on the product itself, if any."""
    raw = (product.get("season") or "").strip()
    if not raw:
        return []
    listed = []
    if raw.startswith("["):
        try:
            listed = [str(x) for x in json.loads(raw) if x]
        except ValueError:
            return []
    else:
        listed = [raw]
    out = []
    for name in listed:
        match = next((s for s in SEASONS if s.lower() in name.lower()), None)
        out.append(match or ("All year" if "all year" in name.lower() else name))
    return out


async def learn(products: list[dict]) -> dict[str, dict]:
    """Read the pieces we have not read yet. Cached for the life of the process."""
    unread = [p for p in products if p.get("handle") and p["handle"] not in _known]
    if not unread:
        return _known
    from app.agent.base import build_llm

    model = build_llm(temperature=0, max_tokens=1500)
    for start in range(0, len(unread), BATCH):
        chunk = unread[start:start + BATCH]
        lines = "\n".join(
            f'- {p["handle"]}: {p.get("title", "")} | {(p.get("about") or p.get("description") or "")[:110]}'
            for p in chunk)
        try:
            answer = await model.ainvoke(
                _ASK.format(pieces=lines),
                # This runs inside a shopper's turn; without its own callbacks the
                # JSON would stream to them ahead of their answer.
                config={"callbacks": [], "tags": ["suits"], "run_name": "learn_suits"})
            text = answer.content if hasattr(answer, "content") else str(answer)
            block = re.search(r"\{.*\}", text, re.S)
            read = json.loads(block.group(0)) if block else {}
        except Exception:  # noqa: BLE001 - never fail a shopper's turn over this
            logger.warning("Could not read what %d pieces are for", len(chunk), exc_info=True)
            read = {}
        for piece in chunk:
            found = read.get(piece["handle"]) or {}
            _known[piece["handle"]] = {
                "occasions": [o for o in (found.get("occasions") or []) if o in OCCASIONS],
                "seasons": [s for s in (found.get("seasons") or []) if s in SEASONS],
            }
    return _known


def occasions_of(product: dict) -> list[str]:
    return (_known.get(product.get("handle") or "") or {}).get("occasions") or []


def seasons_of(product: dict) -> list[str]:
    """The merchant's own answer where they gave one, ours otherwise."""
    if stated := _stated_seasons(product):
        return stated
    return (_known.get(product.get("handle") or "") or {}).get("seasons") or []


def _stem(word: str) -> str:
    """Fold the plural, including the -ies kind: "Parties" and "party" have to
    meet, or a wedding dress never counts as party wear."""
    word = word.strip().lower()
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _label(wanted: str) -> str | None:
    """Their word as one of ours. "wedding" is "Weddings"; plurals cost us a
    whole shelf of wedding dresses once, because neither string contained the
    other."""
    said = {_stem(w) for w in re.findall(r"[a-z]+", (wanted or "").lower())}
    for name in OCCASIONS:
        if {_stem(w) for w in name.split()} & said:
            return name
    # They said "beach", "school", "festive" - a word for an occasion rather
    # than its name. The old keyword table is still the floor for reading THEIR
    # words; what it must no longer do is decide what a product is.
    from app.services import occasions as words

    return words.named(wanted)


def score(product: dict, wanted: str | None) -> int:
    """2 for the occasion asked for, 1 for one that neighbours it, 0 otherwise."""
    if not wanted:
        return 0
    have = set(occasions_of(product))
    if not have:
        return 0
    label = _label(wanted) or wanted
    if label in have:
        return 2
    return 1 if have & set(NEIGHBOURS.get(label, ())) else 0


def is_sleepwear(product: dict) -> bool:
    return "Sleepwear" in occasions_of(product)


def suits_season(product: dict, season: str | None) -> bool | None:
    """Whether to RULE THIS OUT for the season - and only where the merchant said.

    Ruling a piece out is a strong act: asked what a three year old should wear
    in winter, reading every piece and keeping only the ones read as winter left
    one shirt. Our own reading orders the shelf (see seasons_of); it does not
    empty it. Only the merchant's own season metafield excludes.
    """
    if not season:
        return True
    stated = _stated_seasons(product)
    if not stated:
        return None
    return any(season.lower() in s.lower() or "all year" in s.lower() for s in stated)
