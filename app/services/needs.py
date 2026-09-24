"""What the shopper has told us they need, read straight off their own words.

The storefront shows it back to them - "Understood: Age 5 years, Occasion
Birthday party, Budget Around £400, Size 5Y" - and draws the same values as
"Searching for" chips above the results. It is a mirror, not a decision: the
agent still reads the conversation itself and chooses the products. So this is
deliberately plain pattern matching over the shopper's messages. It costs no
model call, it cannot invent a value nobody said, and a later message simply
overwrites an earlier one ("show me something in pink" replaces "blue").
"""

import re

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_NUM = r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"

# "my 5-year-old", "5 years old", "age 6", "she is six", "18 months"
_AGE_YEARS = re.compile(rf"\b{_NUM}[\s-]*(?:years?|yrs?|yo|y/o)(?:[\s-]*old)?\b", re.I)
# "she is 6", "my daughter is 6", "the little one is 6", "turning 7"
_AGE_PLAIN = re.compile(
    rf"\b(?:aged?|turning|(?:she|he|they|daughter|son|girl|boy|baby|child|kid|one)"
    rf"(?:'?s| is| are)?)\s+{_NUM}\b",
    re.I,
)
_AGE_MONTHS = re.compile(r"\b(\d{1,2})[\s-]*(?:months?|mths?|mos?)(?:[\s-]*old)?\b", re.I)

# "size 5Y", "5-6Y", "6-7 years" written as a size, "size 6"
_SIZE = re.compile(r"\b(?:size\s+)?(\d{1,2}\s*-\s*\d{1,2}\s*y|\d{1,2}\s*y)\b|\bsize\s+(\d{1,2})\b", re.I)

# "under £200", "around £400", "£150 budget", "budget of 200", "up to 150"
_BUDGET = re.compile(
    r"(?:(under|below|less than|up to|max(?:imum)?|around|about|roughly|approx(?:imately)?|budget(?: of| is)?)\s*)?"
    r"([£$€₹])\s?(\d{2,6}(?:\.\d{2})?)"
    # ...or no sign at all: "around 30000", "budget of 500". Three figures at
    # least, so an age or a size is never read as money.
    r"|(?:(under|below|less than|up to|max(?:imum)?|around|about|roughly|approx(?:imately)?|budget(?: of| is)?)\s*)(\d{3,6})",
    re.I,
)

_WHO = [
    ("Girl", ("daughter", "girl", "girls", "granddaughter", "niece", "goddaughter", "her", "she")),
    ("Boy", ("son", "boy", "boys", "grandson", "nephew", "godson", "his", "he")),
    ("Baby", ("baby", "newborn", "infant")),
]

# Longest first, so "birthday party" wins over "party" and "family dinner" over "dinner".
_OCCASIONS = [
    "flower girl", "page boy", "birthday party", "family dinner", "first communion",
    "christening", "baptism", "wedding", "bridesmaid", "birthday", "christmas", "easter",
    "eid", "diwali", "party", "school", "holiday", "photoshoot", "portrait", "everyday",
    "celebration", "church", "garden party", "dinner",
]
_OCCASIONS.sort(key=len, reverse=True)

_STYLES = [
    ("Smart-casual", ("smart-casual", "smart casual")),
    ("Formal", ("formal", "occasion wear", "dressy")),
    ("Party", ("party",)),
    ("Classic", ("classic", "traditional", "timeless")),
    ("Casual", ("casual", "everyday", "relaxed")),
    ("Premium", ("premium", "luxury", "special", "more expensive")),
]

# Colours only. "Gingham", "tartan" and "floral" are patterns, and they arrive
# inside product names - "the Catherine Gingham dress" was read as a colour and
# quietly replaced the pink the shopper had actually asked for.
_COLOURS = [
    "navy", "blue", "pink", "white", "ivory", "cream", "red", "burgundy", "green", "sage",
    "yellow", "lilac", "purple", "grey", "gray", "black", "beige", "gold", "silver",
    "orange", "mint", "coral", "rose", "camel", "denim", "khaki", "brown", "teal",
]

# Words that stop a colour being read as one: "blue" in "baby blue" is still blue,
# but "rose" in "Rose dress" is a product name, so colours only count after a
# colour-ish cue or on their own.
_COLOUR_RE = re.compile(r"\b(" + "|".join(_COLOURS) + r")\b", re.I)

# "it's summer now" is a constraint, not small talk: it rules out the wool
# coats. Weather words count as the season they belong to.
_SEASONS = [
    ("Summer", ("summer", "hot weather", "heatwave", "sunny", "the heat", "holiday season")),
    ("Winter", ("winter", "cold weather", "freezing", "snow", "chilly")),
    ("Spring", ("spring",)),
    ("Autumn", ("autumn", "fall ", "back to school")),
    ("Monsoon", ("monsoon", "rainy season", "rains")),
]


def _season(text: str) -> str | None:
    lowered = text.lower()
    return next((name for name, words in _SEASONS if any(w in lowered for w in words)), None)


FIELD_ORDER = ["for", "age", "occasion", "season", "style", "colour", "budget", "size"]
# What is worth remembering between visits: who they shop for and her size.
# Occasion and budget belong to one shopping trip, not the next.
REMEMBERED = ("for", "age", "size", "colour")
LABELS = {
    "for": "For", "age": "Age", "occasion": "Occasion", "style": "Style",
    "colour": "Colour", "budget": "Budget", "size": "Size", "season": "Season",
}


def _number(raw: str) -> int | None:
    raw = raw.lower()
    if raw.isdigit():
        return int(raw)
    return _NUMBER_WORDS.get(raw)


def _age(text: str) -> tuple[str, int | None] | None:
    """("5 years", 5) or ("18 months", None). None when no age was given."""
    if m := _AGE_MONTHS.search(text):
        months = int(m.group(1))
        if months < 36:
            return f"{months} months", None
    for pattern in (_AGE_YEARS, _AGE_PLAIN):
        if m := pattern.search(text):
            years = _number(m.group(1))
            if years is not None and 0 < years <= 16:
                return f"{years} year{'s' if years != 1 else ''}", years
    return None


def _size(text: str) -> str | None:
    m = _SIZE.search(text)
    if not m:
        return None
    if m.group(1):
        return re.sub(r"\s+", "", m.group(1)).upper()
    return f"{m.group(2)}Y"


# Enough to print a figure the way the storefront does. Anything not listed
# is shown as its code ("AED 300"), which is still right, just less pretty.
_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "INR": "₹", "AUD": "A$", "CAD": "C$",
            "NZD": "NZ$", "JPY": "¥", "CHF": "CHF ", "SEK": "kr ", "AED": "AED ", "SGD": "S$"}


def symbol(currency: str | None) -> str:
    """The sign the shopper's own storefront prints. "" when we do not know."""
    code = (currency or "").strip().upper()
    if not code:
        return ""
    return _SYMBOLS.get(code, f"{code} ")


def _budget(text: str, default_symbol: str = "") -> str | None:
    m = _BUDGET.search(text)
    if not m:
        return None
    if m.group(3):
        qualifier, symbol_, amount = (m.group(1) or "").lower(), m.group(2), m.group(3)
    else:
        qualifier, symbol_, amount = (m.group(4) or "").lower(), default_symbol, m.group(5)
    qualifier = qualifier.replace("budget of", "").replace("budget is", "").replace("budget", "").strip()
    # A figure typed with no sign at all is in the money the shop is charging
    # them. A sign they DID type is theirs and stays: turning "£400" into "₹400"
    # silently made a budget a hundredth of what they meant.
    if not m.group(2) and not m.group(4):
        symbol_ = default_symbol or symbol_
    amount = amount[:-3] if amount.endswith(".00") else amount
    if qualifier in ("under", "below", "less than", "up to", "max", "maximum"):
        return f"Under {symbol_}{amount}"
    return f"Around {symbol_}{amount}"


def _first_in(text: str, table) -> str | None:
    """The first label whose words appear in the text as whole words."""
    lowered = text.lower()
    for label, words in table:
        if any(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", lowered) for w in words):
            return label
    return None


def _occasion(text: str) -> str | None:
    lowered = text.lower()
    for occasion in _OCCASIONS:
        if re.search(rf"\b{re.escape(occasion)}\b", lowered):
            return occasion.capitalize()
    return None


def _colour(text: str) -> str | None:
    found = _COLOUR_RE.findall(text)
    if not found:
        return None
    colour = found[-1].lower()
    return "Grey" if colour == "gray" else colour.capitalize()


def understood(messages: list[str], base: dict | None = None, currency: str | None = None) -> dict:
    """Everything the shopper has told us, latest mention winning.

    ``messages`` are the shopper's own messages, oldest first. Returns
    {"fields": [{"key", "label", "value"}], "age": int|None} with fields in a
    fixed display order, or no fields when nothing was said yet.
    """
    found: dict[str, str] = {k: v for k, v in (base or {}).items() if k in REMEMBERED and v}
    age_years: int | None = None
    if found.get("age") and (m := re.match(r"(\d{1,2}) year", found["age"])):
        age_years = int(m.group(1))
    said_age = said_size = False
    for text in messages:
        if not text or not text.strip():
            continue
        if who := _first_in(text, _WHO):
            found["for"] = who
        if age := _age(text):
            found["age"], age_years = age[0], age[1]
            said_age = True
        if occasion := _occasion(text):
            found["occasion"] = occasion
        if season := _season(text):
            found["season"] = season
        if style := _first_in(text, _STYLES):
            found["style"] = style
        if colour := _colour(text):
            found["colour"] = colour
        if budget := _budget(text, symbol(currency)):
            found["budget"] = budget
        if size := _size(text):
            found["size"] = size
            said_size = True

    # An age with no size given is still a size: a 5-year-old wears 5Y.
    # A newly given age replaces a size remembered from an older visit, too.
    if age_years and ("size" not in found or (said_age and not said_size)):
        found["size"] = f"{age_years}Y"

    # "Party" as a style beside "Birthday party" says the same thing twice.
    style = found.get("style", "").lower()
    if style and style in found.get("occasion", "").lower():
        found.pop("style")

    return {
        "fields": [{"key": k, "label": LABELS[k], "value": found[k]} for k in FIELD_ORDER if k in found],
        "age": age_years,
    }
