"""What the shopper has told us, read by the model rather than by word lists.

The Understood panel, the "Searching for" chips and the facts every tool falls
back on (who it is for, their age, size, colour, budget) used to come from
regular expressions. "My daughter's friend turns 5 - a gift for him" came out
as For: Girl. A short model call reads the conversation the way a shop
assistant would - the person the purchase is FOR, not whoever is mentioned -
and the word lists are kept only as a fallback if that call fails.
"""

import asyncio
import json
import logging
import re

from app.services import needs

logger = logging.getLogger(__name__)

# Room for the backup model when the main one is down: cut off at 8s, the
# word lists read "she doesn't like pink" as Colour: Pink.
TIMEOUT_SECONDS = 20

_PROMPT = """You read a shopper's messages to a children's clothing shop and note what they want.
Return ONLY a JSON object with these keys, each a short string or null when not said:
- "for": "Girl", "Boy" or "Baby" - the person the purchase is FOR (the recipient). Baby only
  for a baby whose sex is not given. Only when it is clear: if the messages leave it open or
  contradict themselves ("my daughter ... her friend ... a gift for him"), "for" is null.
- "unsure": "for" when who it is for is unclear or contradictory, else null. Shopping for
  several children whom they name clearly ("twins, a boy and a girl") is not unclear:
  "for" and "unsure" are both null.
- "age": the recipient's age as "5 years" or "3 months" ("a newborn" is "0 months").
- "occasion": e.g. "Birthday", "Wedding", "School", "Christmas".
- "season": "Summer" or "Winter" when they say so.
- "style": a style word they used, e.g. "Smart", "Casual".
- "colour": one colour they asked for, capitalised, e.g. "Navy".
- "avoid_colour": a colour they turned down ("she doesn't like pink" -> "Pink"); kept until
  they ask for it again. Never the same as "colour".
- "budget": "Under {sym}15000" or "Around {sym}400" - their own currency sign if they used one,
  otherwise {sym}.
- "size": a clothing size only if they named one, e.g. "5Y", "18M", "5-6Y".
- "category": the kind of piece they want, singular, e.g. "Dress", "Shirt", "Shoes" - kept
  until they ask for a different kind.
- "count": how many items their LATEST message asks to see, as a number ("show me 2 jackets"
  -> 2), else null. Never an age, a size, a price or a number of children.
The latest message wins when they change something.
A DIFFERENT CHILD: when the newest messages are about another child than before (another age,
or a size that could not fit the earlier child - a baby size after a 14 year old), describe ONLY
the newest child. Everything said about the earlier child is dropped - its age, size, occasion,
colour, style, category and budget - unless the newest messages say it again. "for" then comes
from the newest messages alone ("for my boy, 3 months" -> "Boy"), null if they do not say.{remembered}"""


def _fields(found: dict) -> dict:
    raw_count = found.get("count")
    count = raw_count if isinstance(raw_count, int) and 0 < raw_count <= 12 else None
    unsure = str(found.get("unsure") or "").strip().lower() or None
    found = {k: str(v).strip() for k, v in found.items() if k in needs.FIELD_ORDER and v and str(v).strip()}
    if unsure == "for":
        found.pop("for", None)
    # A colour they turned down is no longer the colour they want.
    if found.get("avoid_colour") and found.get("colour", "").lower() == found["avoid_colour"].lower():
        found.pop("colour")
    # The rest of the shop reads a budget as "Under ₹15000" / "Around £400".
    if (b := found.get("budget")) and not re.match(r"(?i)(under|around)\b", b):
        found["budget"] = f"Under {b}"
    age_years = None
    if m := re.match(r"(\d{1,2})\s*year", found.get("age", "")):
        age_years = int(m.group(1))
    # An age with no size given is still a size: a 5 year old wears 5Y.
    if age_years and "size" not in found:
        found["size"] = f"{age_years}Y"
    # A baby's age in months is a size too: "3 months" wears 3M.
    elif (m := re.match(r"(\d{1,2})\s*month", found.get("age", ""))) and "size" not in found:
        found["size"] = f"{int(m.group(1))}M"
    return {
        "fields": [{"key": k, "label": needs.LABELS[k], "value": found[k]}
                   for k in needs.FIELD_ORDER if k in found],
        "age": age_years,
        "count": count,
        "unsure": unsure,
    }


async def understood(messages: list[str], base: dict | None = None, currency: str | None = None) -> dict:
    """needs.understood(), read by the model. Same shape; falls back to it on failure."""
    said = [m for m in messages if m and m.strip()]
    if not said:
        return needs.understood(messages, base=base, currency=currency)
    sym = needs.symbol(currency) or ""
    kept = {k: v for k, v in (base or {}).items() if k in needs.REMEMBERED and v}
    remembered = ("\nRemembered from an earlier visit (keep unless the messages change it): "
                  + json.dumps(kept)) if kept else ""
    try:
        from app.agent.base import build_llm
        llm = build_llm(temperature=0, max_tokens=200)
        answer = await asyncio.wait_for(llm.ainvoke([
            ("system", _PROMPT.format(sym=sym, remembered=remembered)),
            ("human", "\n".join(f"- {m}" for m in said[-24:])),
        ]), timeout=TIMEOUT_SECONDS)
        text = answer.content if isinstance(answer.content, str) else str(answer.content)
        found = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        if not isinstance(found, dict):
            raise ValueError("not an object")
        return _fields(found)
    except Exception:  # noqa: BLE001 - the panel must never cost the answer
        logger.warning("Model read of the shopper's needs failed; using the word lists", exc_info=True)
        return needs.understood(messages, base=base, currency=currency)
