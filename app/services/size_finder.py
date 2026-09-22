"""Smart Size Finder: age, height, chest and usual size in, one size out.

Four answers from the quiz - or the same facts dropped into a chat message - are
turned into a single recommendation such as "6-7Y", with the fit it will give
and why. The chart is the standard UK childrenswear one (each size is cut for a
child up to that height and chest), and the product's own fit moves the answer:
a smocked bodice or a piece tagged ``fit:close`` sits closer, so a child at the
top of a size is sized up; a piece tagged ``fit:roomy`` is not.

The answer is always one of the sizes the product is actually sold in, so the
widget can add exactly that variant to the bag.
"""

import re
from dataclasses import dataclass

from app.services import outfit
from app.services.shopify_storefront import product_image, product_url


@dataclass(frozen=True)
class Band:
    label: str          # "3M", "2Y"
    height_cm: int      # tallest child the size is cut for
    chest_cm: int       # fullest chest the size is cut for
    shoe_eu: int        # a typical shoe size at this age, for footwear


# UK childrenswear, newborn to 12 years, one rung per size a shop sells.
CHART = [
    Band("1M", 56, 40, 16), Band("3M", 62, 42, 17), Band("6M", 68, 44, 18), Band("9M", 74, 46, 19),
    Band("12M", 80, 48, 20), Band("18M", 86, 50, 21), Band("2Y", 92, 53, 23), Band("3Y", 98, 54, 25),
    Band("4Y", 104, 56, 27), Band("5Y", 110, 58, 28), Band("6Y", 116, 60, 30), Band("7Y", 122, 62, 31),
    Band("8Y", 128, 64, 32), Band("9Y", 134, 66, 33), Band("10Y", 140, 68, 34), Band("11Y", 146, 71, 35),
    Band("12Y", 152, 74, 36),
]
INDEX = {b.label: i for i, b in enumerate(CHART)}
TOP = len(CHART) - 1

# Within this many cm of a size's limit, a close-fitting piece is sized up.
CLOSE_FIT_MARGIN_CM = 2
# Answers more than this many sizes apart describe different children - one of
# them is a typo, and a size picked from the mix would fit nobody.
MAX_SPREAD = 3

CLOSE_WORDS = ("smock", "fitted", "slim", "bodice", "tailored")
ROOMY_WORDS = ("relaxed", "oversized", "loose", "roomy", "swing", "trapeze")


def _clamp(i: int) -> int:
    return max(0, min(TOP, i))


def _index_for(value: float, attr: str) -> int:
    """The smallest size cut for a child this tall (or this full in the chest)."""
    for i, band in enumerate(CHART):
        if value <= getattr(band, attr):
            return i
    return TOP


def _near_limit(value: float, i: int, attr: str) -> bool:
    return getattr(CHART[i], attr) - value < CLOSE_FIT_MARGIN_CM


def _label_index(value: int, unit: str) -> int:
    """(18, "M") -> the 18M rung; (5, "Y") -> 5Y; months past 24 count as years."""
    if unit == "M":
        if value >= 24:
            return INDEX.get(f"{value // 12}Y", TOP)
        months = [(int(b.label[:-1]), i) for i, b in enumerate(CHART) if b.label.endswith("M")]
        fit = [i for m, i in months if value <= m]
        return fit[0] if fit else INDEX["2Y"]
    return _clamp(INDEX.get(f"{value}Y", INDEX["2Y"] + value - 2))


def span_of(raw: str | None) -> tuple[int, int] | None:
    """A size label as (low, high) rungs: "5-6Y" -> (5Y, 6Y), "18-24M" -> (18M, 2Y), "3M"."""
    if not raw:
        return None
    text = raw.strip().upper().replace("–", "-").replace(" ", "")
    if m := re.fullmatch(r"(\d{1,2})-(\d{1,2})(M|Y|YRS?|YEARS?|MTHS?|MONTHS?)?", text):
        unit = "M" if (m.group(3) or "Y").startswith("M") else "Y"
        return _label_index(int(m.group(1)), unit), _label_index(int(m.group(2)), unit)
    if m := re.fullmatch(r"(?:AGE)?(\d{1,2})(M|Y|YRS?|YEARS?|MTHS?|MONTHS?)?", text):
        unit = "M" if (m.group(2) or "Y").startswith("M") else "Y"
        i = _label_index(int(m.group(1)), unit)
        return i, i
    return None


parse_usual = span_of


def fit_of(product: dict | None) -> tuple[str, str | None]:
    """("close" | "true" | "roomy", a sentence on why) from the product's own words."""
    if not product:
        return "true", None
    tags = {t.strip().lower() for t in product.get("tags") or []}
    words = " ".join([product.get("title") or "", product.get("description") or ""]).lower()
    if "fit:close" in tags or any(w in words for w in CLOSE_WORDS):
        why = ("Smocked bodices sit closer" if "smock" in words else "This style is cut close")
        return "close", why
    if "fit:roomy" in tags or any(w in words for w in ROOMY_WORDS):
        return "roomy", "This style is cut with room to move"
    return "true", None


def _size_label(raw: str) -> str:
    return raw.strip().upper().replace("–", "-")


def _is_shoe_size(label: str) -> bool:
    return bool(re.fullmatch(r"(?:EU\s*)?\d{2}(?:\.5)?", label.strip().upper()))


def _pick_offered(target: int, offered: list[str]) -> str | None:
    """The sold size that fits a child needing rung ``target``.

    Ranges overlap ("5-6Y", "6-7Y"): a child who needs 6 goes into the one that
    STARTS at 6, so there is a year to grow.
    """
    spans = [(span_of(label), label) for label in offered]
    spans = [(sp, label) for sp, label in spans if sp]
    covering = [(sp, label) for sp, label in spans if sp[0] <= target <= sp[1]]
    if covering:
        covering.sort(key=lambda c: (c[0][0] != target, c[0][1] - c[0][0]))
        return covering[0][1]
    above = sorted((sp, label) for sp, label in spans if sp[0] > target)
    if above:
        return above[0][1]
    return max(spans)[1] if spans else None


def recommend(age: float | None = None, height_cm: float | None = None,
              chest_cm: float | None = None, usual_size: str | None = None,
              product: dict | None = None) -> dict:
    """One size, from whatever the shopper could tell us.

    ``product`` is an ACTIVE product (title, tags, description, options);
    without one the answer is a plain chart size such as "6M" or "5Y".
    """
    usual = span_of(usual_size)
    said: dict[str, int] = {}
    if height_cm:
        said["height"] = _index_for(height_cm, "height_cm")
    if chest_cm:
        said["chest"] = _index_for(chest_cm, "chest_cm")
    if usual:
        said["usual size"] = usual[0]
    if age:
        said["age"] = _label_index(round(age * 12), "M") if age < 2 else _label_index(int(age), "Y")
    if not said:
        return {"found": False, "still_to_ask": ["age", "height"],
                "tell_customer": "Tell me their age and height and I'll find the right size."}

    # Answers that point at sizes far apart are a typo somewhere, not a child.
    if max(said.values()) - min(said.values()) > MAX_SPREAD:
        return {
            "found": False,
            "reason": "answers_disagree",
            "points_to": {k: CHART[v].label for k, v in said.items()},
            "tell_customer": ("Those answers point to very different sizes ("
                              + ", ".join(f"{k} {CHART[v].label}" for k, v in said.items())
                              + "). Could you check them? Height and usual size matter most."),
        }

    # Measurements beat labels: they are the child, a usual size is a brand's idea.
    measured = [said[k] for k in ("height", "chest") if k in said]
    target = _clamp(max(measured) if measured else max(said.values()))
    fit, fit_why = fit_of(product)
    reasons: list[str] = []
    sized_up = False
    near_top = ((height_cm and _near_limit(height_cm, target, "height_cm"))
                or (chest_cm and _near_limit(chest_cm, target, "chest_cm"))
                or (usual and usual[1] > target))
    if fit == "close" and (near_top or not measured):
        target, sized_up = _clamp(target + 1), True
        reasons.append(f"{fit_why}, so we have sized up.")
    elif fit == "roomy":
        reasons.append(f"{fit_why}, so their usual size will do.")

    offered = [_size_label(v) for v in outfit._options_of(product or {}).get("Size", [])] if product else []
    if offered and all(_is_shoe_size(x) for x in offered):
        eu = CHART[target].shoe_eu
        recommended = min(offered, key=lambda x: abs(float(re.sub(r"[^\d.]", "", x)) - eu))
        reasons = ["Shoe sizes vary by foot length, so this is our best guess for their age."]
    elif offered:
        recommended = _pick_offered(target, offered) or CHART[target].label
    else:
        recommended = CHART[target].label
    alternatives = (_neighbours(recommended, offered) if offered
                    else [CHART[i].label for i in (target - 1, target, target + 1) if 0 <= i <= TOP])
    if not reasons:
        reasons.append("A true-to-size fit with a little room to grow.")
    band = CHART[target].label
    return {
        "found": True,
        "recommended": recommended,
        "age_label": f"Age {band[:-1]} {'months' if band.endswith('M') else ''}".strip() if band.endswith("M")
                     else f"Age {band[:-1]}",
        "fit": "close" if sized_up else fit,
        "fit_note": " ".join(reasons),
        "sized_up": sized_up,
        "alternatives": alternatives,
        "answers": {
            "age": f"{age:g} years" if age else None,
            "height": f"{height_cm:g} cm" if height_cm else None,
            "chest": f"{chest_cm:g} cm" if chest_cm else None,
            "usual_size": usual_size or None,
        },
        "sizes_offered": offered,
    }


def _neighbours(pick: str, offered: list[str]) -> list[str]:
    """The picked size with the ones either side of it, in the order sold."""
    if pick not in offered:
        return [pick]
    i = offered.index(pick)
    return offered[max(0, i - 1): i + 2]


def _matches(product: dict, wanted: str) -> bool:
    wanted = wanted.strip().lower()
    return wanted in (product.get("handle") or "").lower() or wanted in (product.get("title") or "").lower()


async def find_product(name_or_handle: str | None) -> dict | None:
    """The ACTIVE product the shopper means, by handle or by (part of) its title."""
    if not name_or_handle or not name_or_handle.strip():
        return None
    handle = name_or_handle.strip().lower()
    if re.fullmatch(r"[a-z0-9-]+", handle):
        found = await outfit._active_products([handle])
        if found:
            return found[0]
    products = await outfit._active_products()
    exact = [p for p in products if _matches(p, name_or_handle)]
    if exact:
        return exact[0]
    # Every word of what they said appears in the title.
    words = [w for w in re.findall(r"[a-z0-9]+", name_or_handle.lower()) if len(w) > 2]
    loose = [p for p in products if words and all(w in (p.get("title") or "").lower() for w in words)]
    return loose[0] if loose else None


async def for_product(product_ref: str | None = None, **answers) -> dict:
    """recommend() for the product the shopper named, with it attached for the card."""
    product = await find_product(product_ref)
    result = recommend(product=product, **answers)
    if product and result.get("found"):
        result["product"] = {
            "title": product.get("title"),
            "handle": product.get("handle"),
            "product_id": product.get("legacyResourceId"),
            "image": product_image(product),
            "url": product_url(product),
            "variants": [
                {"variant_id": v.get("legacyResourceId"),
                 "size": outfit._option_value(v, "Size"),
                 "price": v.get("price"),
                 "available": v.get("availableForSale")}
                for v in (product.get("variants") or {}).get("nodes") or []
            ],
        }
    elif product_ref and result.get("found"):
        result["product_not_found"] = product_ref
    return result
