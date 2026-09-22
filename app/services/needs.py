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
_AGE_PLAIN = re.compile(rf"\b(?:aged?|she'?s|he'?s|she is|he is|turning)\s+{_NUM}\b", re.I)
_AGE_MONTHS = re.compile(r"\b(\d{1,2})[\s-]*(?:months?|mths?|mos?)(?:[\s-]*old)?\b", re.I)

# "size 5Y", "5-6Y", "6-7 years" written as a size, "size 6"
_SIZE = re.compile(r"\b(?:size\s+)?(\d{1,2}\s*-\s*\d{1,2}\s*y|\d{1,2}\s*y)\b|\bsize\s+(\d{1,2})\b", re.I)

# "under £200", "around £400", "£150 budget", "budget of 200", "up to 150"
_BUDGET = re.compile(
    r"(?:(under|below|less than|up to|max(?:imum)?|around|about|roughly|approx(?:imately)?|budget(?: of| is)?)\s*)?"
    r"([£$€])\s?(\d{2,5}(?:\.\d{2})?)"
    r"|budget(?: of| is)?\s*([£$€])?\s?(\d{2,5})",
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

_COLOURS = [
    "navy", "blue", "pink", "white", "ivory", "cream", "red", "burgundy", "green", "sage",
    "yellow", "lilac", "purple", "grey", "gray", "black", "beige", "gold", "silver",
    "floral", "gingham", "tartan", "orange", "mint", "coral", "rose",
]

# Words that stop a colour being read as one: "blue" in "baby blue" is still blue,
# but "rose" in "Rose dress" is a product name, so colours only count after a
# colour-ish cue or on their own.
_COLOUR_RE = re.compile(r"\b(" + "|".join(_COLOURS) + r")\b", re.I)

FIELD_ORDER = ["for", "age", "occasion", "style", "colour", "budget", "size"]
LABELS = {
    "for": "For", "age": "Age", "occasion": "Occasion", "style": "Style",
    "colour": "Colour", "budget": "Budget", "size": "Size",
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


def _budget(text: str) -> str | None:
    m = _BUDGET.search(text)
    if not m:
        return None
    if m.group(3):
        qualifier, symbol, amount = (m.group(1) or "").lower(), m.group(2), m.group(3)
    else:
        qualifier, symbol, amount = "", m.group(4) or "£", m.group(5)
    amount = amount[:-3] if amount.endswith(".00") else amount
    if qualifier in ("under", "below", "less than", "up to", "max", "maximum"):
        return f"Under {symbol}{amount}"
    return f"Around {symbol}{amount}"


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


def understood(messages: list[str]) -> dict:
    """Everything the shopper has told us, latest mention winning.

    ``messages`` are the shopper's own messages, oldest first. Returns
    {"fields": [{"key", "label", "value"}], "age": int|None} with fields in a
    fixed display order, or no fields when nothing was said yet.
    """
    found: dict[str, str] = {}
    age_years: int | None = None
    for text in messages:
        if not text or not text.strip():
            continue
        if who := _first_in(text, _WHO):
            found["for"] = who
        if age := _age(text):
            found["age"], age_years = age[0], age[1]
        if occasion := _occasion(text):
            found["occasion"] = occasion
        if style := _first_in(text, _STYLES):
            found["style"] = style
        if colour := _colour(text):
            found["colour"] = colour
        if budget := _budget(text):
            found["budget"] = budget
        if size := _size(text):
            found["size"] = size

    # An age with no size given is still a size: a 5-year-old wears 5Y.
    if "size" not in found and age_years:
        found["size"] = f"{age_years}Y"

    # "Party" as a style beside "Birthday party" says the same thing twice.
    style = found.get("style", "").lower()
    if style and style in found.get("occasion", "").lower():
        found.pop("style")

    return {
        "fields": [{"key": k, "label": LABELS[k], "value": found[k]} for k in FIELD_ORDER if k in found],
        "age": age_years,
    }
