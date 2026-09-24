"""Which part of an outfit a piece plays, worked out rather than written down.

A shopper asking for an outfit means one thing on top, one on the legs, shoes,
and something to finish it. To honour that we have to know what each product IS,
and a shop does not say: this one files near-identical garments under "Shirt",
"Trousers", "Shorts" and nothing at all, so "one per product type" answered an
outfit request with two shirts, two bottoms and no shoes.

The obvious fix is a table mapping every product type to a part. It would work,
and it would be wrong: it is my vocabulary imposed on someone else's shop, and
it goes stale the first time a merchant adds "Playsuits". So the shop's own
words are read instead, in this order:

  1. Shopify's product taxonomy, where the merchant has set it. It is
     authoritative and nobody has to guess.
  2. Otherwise the model is asked, ONCE, what part each product type plays -
     twenty-odd words, not ninety products - and the answer is cached.
  3. If that cannot be reached, the piece is left unclassified rather than
     guessed at, and the caller falls back to showing what it has.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

# The parts an outfit is made of. Not a vocabulary of garments - a vocabulary of
# ROLES, which is the thing that does not vary between shops.
PARTS = ("Top", "Bottoms", "Dress", "Outerwear", "Shoes", "Accessory", "Other")

# Shopify's taxonomy names the part in its own path: "... > Baby & Children's
# Tops > Shirts", "... > Clothing Accessories > Belts". Read from the most
# specific segment backwards, so "Dresses" beats the "Clothing" above it.
_TAXONOMY = (
    ("Shoes", ("footwear", "shoes", "boots", "sandals", "sneakers")),
    ("Dress", ("dresses", "one-pieces", "rompers", "overalls")),
    ("Outerwear", ("outerwear", "coats & jackets", "coats", "jackets")),
    ("Bottoms", ("bottoms", "pants", "trousers", "shorts", "skirts")),
    ("Top", ("tops", "shirts", "sweaters", "t-shirts")),
    ("Accessory", ("accessories", "belts", "hats", "socks", "hosiery", "jewelry")),
)

_ASK = (
    "A children's clothing shop files its products under these product types:\n"
    "{types}\n\n"
    "For each one, say which part of an outfit it is. Use exactly one of: "
    "Top, Bottoms, Dress, Outerwear, Shoes, Accessory, Other.\n"
    "Trousers and shorts are both Bottoms. A romper or an all-in-one is a Dress, "
    "because it covers top and bottoms at once. Tights, socks, hats, belts and "
    "hairbands are Accessory. Anything not worn - a toy, a gift card - is Other.\n"
    "Answer with JSON only: {{\"<product type>\": \"<part>\", ...}}"
)

_learned: dict[str, str] = {}


def from_taxonomy(full_name: str | None) -> str | None:
    """The part named by Shopify's own taxonomy, when the merchant has set it."""
    path = (full_name or "").lower()
    if not path:
        return None
    for segment in reversed([p.strip() for p in path.split(">")]):
        for part, words in _TAXONOMY:
            if any(word in segment for word in words):
                return part
    return None


async def learn(types: list[str]) -> dict[str, str]:
    """Ask the model what part each product type plays. Cached across calls."""
    unknown = sorted({t.strip() for t in types if t and t.strip() not in _learned})
    if not unknown:
        return _learned
    try:
        # Imported here, not at the top: a service reaching into the agent layer
        # at import time is how import cycles start.
        from app.agent.base import build_llm

        # callbacks=[] on purpose: this runs inside a shopper's turn, and
        # LangChain hands every model call the handlers of the run it sits in.
        # Without it, the classifier's JSON was streamed to the shopper, ahead of
        # their actual answer.
        answer = await build_llm(temperature=0, max_tokens=700).ainvoke(
            _ASK.format(types="\n".join(f"- {t}" for t in unknown)),
            config={"callbacks": [], "tags": ["parts"], "run_name": "learn_parts"})
        text = answer.content if hasattr(answer, "content") else str(answer)
        if block := re.search(r"\{.*\}", text, re.S):
            for product_type, part in json.loads(block.group(0)).items():
                if isinstance(part, str) and part.strip().title() in PARTS:
                    _learned[str(product_type).strip()] = part.strip().title()
    except Exception:  # noqa: BLE001 - never fail a shopper's turn over this
        logger.warning("Could not work out the parts for %s", unknown, exc_info=True)
    return _learned


def known(product_type: str | None) -> str | None:
    return _learned.get((product_type or "").strip())
