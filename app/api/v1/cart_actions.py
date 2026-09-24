"""What the shopper wants done with their bag, as one word the widget acts on.

The ``action`` events carry exact instructions - these variant ids, this page -
and only appear when the agent itself called add_to_cart or go_to_checkout. This
is the plainer signal behind them: the shopper asked to add what they were shown,
to check out with it, or to check out with only what is already in the bag. The
widget holds the cards it drew, so it knows which products "these" are.

Read off the shopper's own words first, the way the requested count is: the model
is not relied on to report an intent it may not act on. What the agent did this
turn fills the gap for the replies that name no action - "yes please" to "shall I
add the look to your bag?".
"""

import re

ADD_PREVIOUS = "add previous products in cart"
CHECKOUT = "checkout"
CHECKOUT_FROM_EXISTING = "checkout from existing"

_CART = r"(?:cart|bag|basket|trolley)"
_CHECKOUT_WORD = r"(?:check-?\s?out|pay(?:ment)?)"

# A clause that asks about the cart rather than asking us to act on it - "how do
# I checkout?", "does checkout take UPI?". "Can you..." stays: that is a request.
_QUESTION_RE = re.compile(
    r"^(?:how|what|where|when|why|which|is|are|does|do|did|was|were|has|have)\b"
    r"|\bhow\s+(?:do|does|can|to|would|should)\b"
)

# "don't add these", "not ready to checkout" - the verb is struck out to the end
# of its clause, so "don't add these, just checkout" is still a checkout.
_NEGATED_RE = re.compile(
    r"\b(?:don't|dont|do\s+not|not|never|won't|wont)\s+"
    r"(?:(?:want|wanna|need|going|ready|like|yet)\s+)?(?:to\s+)?"
    r"(?:add|put|check-?\s?out|pay|buy|place)\b[^,.;!?]*"
)

_CHECKOUT_RE = re.compile(
    r"\bcheck-?out\b"
    # Two words is also "look at": "check out this dress", "check out the sale".
    r"|\bcheck\s+out\b(?!\s+(?:this|that|these|those|the|our|some|a|an|more|other|your|what)\b)"
    r"|\b(?:place|confirm)\s+(?:my|the|this|an?)\s+order\b"
    r"|\b(?:buy|pay)\s+now\b"
    r"|\bmake\s+(?:the\s+|a\s+)?payment\b"
    rf"|\b(?:proceed|go|move|ready|want|like|wanna)\s+(?:on\s+)?to\s+(?:the\s+)?{_CHECKOUT_WORD}\b"
    rf"|\btake\s+me\s+to\s+(?:the\s+)?{_CHECKOUT_WORD}\b"
)

_ADD_RE = re.compile(
    # "add all these products in my cart", "put them in my bag", "add to cart"
    rf"\b(?:add|put|pop|throw)\b(?:\s+[\w'-]+){{0,6}}?\s+(?:in|into|to|onto)\s+"
    rf"(?:my\s+|the\s+|our\s+)?(?:shopping\s+)?{_CART}\b"
    # "add them", "add these", "add the look"
    r"|\badd\s+(?:it|them|these|those|this|that|all|everything|both|the\s+(?:look|outfit|lot|set))\b"
)

# They do not want what was shown - checked across the whole message, since "Just
# checkout. I don't need the above." puts it in a sentence of its own.
_REJECT_RE = re.compile(
    r"\b(?:don't|dont|do\s+not|never)\s+(?:need|want|like)\b(?!\s+to\b)"
    r"|\b(?:don't|dont|do\s+not|never)\s+(?:add|include|put)\b"
    r"|\bno\s+need\b"
    r"|\bwithout\s+(?:adding|these|those|them|the\s+(?:above|ones|products?|items?|suggest\w*|recommend\w*))\b"
    r"|\b(?:skip|forget|leave|ignore|drop)\s+(?:about\s+)?(?:these|those|them|it|that|this"
    r"|the\s+(?:above|rest|ones|products?|items?|suggest\w*|recommend\w*|outfit|look))\b"
    r"|\b(?:not|none\s+of)\s+(?:these|those|them|the\s+(?:above|ones|products?|items?))\b"
    # Only what is already there: "what's in my cart", "my existing bag", "I already added them"
    rf"|\bwhat(?:ever)?(?:'s|s|\s+is|\s+i\s+(?:already\s+)?(?:have|had|got))\s+(?:already\s+)?in\s+(?:my|the)\s+{_CART}\b"
    r"|\bwith\s+what(?:ever)?\s+i\s+(?:already\s+)?(?:have|had|got)\b"
    rf"|\b(?:existing|current)\s+(?:{_CART}|items?|products?)\b"
    rf"|\balready\s+(?:have\s+)?in\s+(?:my|the)\s+{_CART}\b"
    r"|\balready\s+(?:added|put|have)\b"
    # "just checkout" - nothing else, the shown products included
    rf"|\b(?:just|only|straight|directly)\s+(?:(?:go|take\s+me|proceed)\s+)?(?:to\s+)?(?:the\s+)?{_CHECKOUT_WORD}\b"
)


def _clauses(text: str) -> list[str]:
    """The parts of the message that are not questions, with negated verbs struck out."""
    parts = (p.strip() for p in re.split(r"[.?!\n]+", text))
    return [_NEGATED_RE.sub(" ", p) for p in parts if p and not _QUESTION_RE.search(p)]


def cart_action(message: str, issued: list[dict] | None = None) -> str | None:
    """ADD_PREVIOUS, CHECKOUT, CHECKOUT_FROM_EXISTING, or None when neither was asked.

    issued: the ``action`` dicts the agent's own tools produced this turn. Checking
    out wins over adding - "add these and checkout" is a checkout with them in it.
    """
    text = " ".join((message or "").lower().replace("’", "'").split())
    clauses = _clauses(text)
    issued = issued or []

    checkout = any(_CHECKOUT_RE.search(c) for c in clauses) or any(
        a.get("type") == "redirect" and a.get("page") == "checkout" for a in issued
    )
    if checkout:
        return CHECKOUT_FROM_EXISTING if _REJECT_RE.search(text) else CHECKOUT
    if any(_ADD_RE.search(c) for c in clauses) or any(a.get("type") == "add_to_cart" for a in issued):
        return ADD_PREVIOUS
    return None


# A message that only picks which one they mean - "choose size 12y", "in 5Y",
# "the navy one". The agent read one of these as consent and put a top in a
# shopper's bag they had never asked for; they may not notice until they pay.
# A bare number counts only where they said "size": "8" on its own is as likely
# to be an age.
_SIZE_TOKEN = (r"(?:\d{1,2}\s?(?:y|yr|yrs|years?|m|mth|mths|months?)|\d{2}\s?eu|"
               r"x?[sml]|xl|xxl|(?<=size\s)\d{1,2})")
_COLOUR_TOKEN = (r"(?:navy|blue|pink|white|cream|ivory|burgundy|red|green|camel|brown|black|"
                 r"grey|gray|beige|teal|yellow|orange|gold|silver|sky\s?blue|khaki|denim)")
_NARROWING_RE = re.compile(
    rf"^(?:i\s+(?:want|like|will\s+take|choose)|let'?s\s+have|make\s+it|show\s+me|"
    rf"give\s+me|go\s+with|choose|select|pick|take|in|size|colour|color|the)?\s*"
    rf"(?:size\s+|colour\s+|color\s+|in\s+)?"
    rf"(?:{_SIZE_TOKEN}|{_COLOUR_TOKEN})(?:\s+one|\s+please|\s+size|\s+pls|\s+then)?[.!]?$",
    re.I,
)


def is_only_narrowing(text: str) -> bool:
    """True when they are saying WHICH one, not asking for it to be bought."""
    said = " ".join((text or "").split())
    if not said or len(said.split()) > 6:
        return False
    if _ADD_RE.search(said) or _CHECKOUT_RE.search(said):
        return False
    return bool(_NARROWING_RE.match(said))
