"""What a piece is worn for, read from the store's own words.

A shopper asking for "a dress for a wedding" is not asking for any dress, and
nothing in Shopify marks a product as wedding-wear. What the store does have is
the language it already writes: a name ("Balmoral Tartan"), its tags, its
description. Those words place a piece at an occasion well enough to rank by,
and the vocabulary below is only the mapping from a word to the occasion it
names - never a claim about a particular product.
"""

GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Weddings", ("wedding", "bridesmaid", "flower girl", "page boy", "bridal")),
    ("Christenings", ("christening", "baptism", "communion", "blessing")),
    ("Parties", ("party", "birthday", "celebration", "celebrate")),
    ("Christmas", ("christmas", "festive", "tartan", "velvet", "yule")),
    ("Occasion wear", ("occasion", "formal", "ceremony", "special", "smart", "dressy", "elegant")),
    ("Holiday", ("holiday", "beach", "summer", "sun", "swim", "vacation")),
    ("Everyday", ("everyday", "play", "casual", "nursery", "school", "weekend", "playdate")),
]

# A piece for a wedding is usually right for a christening too. When the exact
# occasion is not in the store's words, a neighbour is a better answer than a
# random dress - but it is always ranked below an exact match.
NEIGHBOURS: dict[str, tuple[str, ...]] = {
    "Weddings": ("Christenings", "Occasion wear", "Parties"),
    "Christenings": ("Weddings", "Occasion wear"),
    "Parties": ("Occasion wear", "Christmas", "Weddings"),
    "Christmas": ("Parties", "Occasion wear"),
    "Occasion wear": ("Weddings", "Christenings", "Parties", "Christmas"),
    "Holiday": ("Everyday",),
    "Everyday": ("Holiday",),
}


# Worn to bed, whatever else the store files it under: a night dress is a dress
# by product type, and it led the list when a shopper asked for a wedding.
SLEEPWEAR = ("night dress", "nightdress", "nightie", "nightgown", "pyjama", "pajama",
             "sleepsuit", "sleep suit", "sleepwear", "bedtime")


def is_sleepwear(*parts: object) -> bool:
    words = " ".join(str(p or "") for p in parts).lower()
    return any(k in words for k in SLEEPWEAR)


def of(*parts: object) -> list[str]:
    """The occasions the store's own words place this piece at, best first."""
    words = " ".join(str(p or "") for p in parts).lower()
    return [label for label, keys in GROUPS if any(k in words for k in keys)]


def named(text: str | None) -> str | None:
    """The occasion a shopper named, as one of our labels. None if they named none."""
    found = of(text)
    return found[0] if found else None


def score(product_occasions: list[str] | None, wanted: str | None) -> int:
    """2 for the occasion asked for, 1 for one that neighbours it, 0 otherwise."""
    if not wanted:
        return 0
    label = named(wanted) or wanted
    have = set(product_occasions or [])
    if label in have:
        return 2
    return 1 if have & set(NEIGHBOURS.get(label, ())) else 0
