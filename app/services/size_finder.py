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
    age: int            # the "5" in 5Y
    height_cm: int      # tallest child the size is cut for
    chest_cm: int       # fullest chest the size is cut for
    shoe_eu: int        # a typical shoe size at this age, for footwear


# UK high-street childrenswear, 2Y to 12Y.
CHART = [
    Band(2, 92, 53, 23), Band(3, 98, 54, 25), Band(4, 104, 56, 27), Band(5, 110, 58, 28),
    Band(6, 116, 60, 30), Band(7, 122, 62, 31), Band(8, 128, 64, 32), Band(9, 134, 66, 33),
    Band(10, 140, 68, 34), Band(11, 146, 71, 35), Band(12, 152, 74, 36),
]
BY_AGE = {b.age: b for b in CHART}
MIN_AGE, MAX_AGE = CHART[0].age, CHART[-1].age

# Within this many cm of a size's limit, a close-fitting piece is sized up.
CLOSE_FIT_MARGIN_CM = 2

CLOSE_WORDS = ("smock", "fitted", "slim", "bodice", "tailored")
ROOMY_WORDS = ("relaxed", "oversized", "loose", "roomy", "swing", "trapeze")


def _clamp(age: int) -> int:
    return max(MIN_AGE, min(MAX_AGE, age))


def _size_for(value: float, attr: str) -> int:
    """The smallest size cut for a child this tall (or this full in the chest)."""
    for band in CHART:
        if value <= getattr(band, attr):
            return band.age
    return MAX_AGE


def _near_limit(value: float, age: int, attr: str) -> bool:
    return getattr(BY_AGE[age], attr) - value < CLOSE_FIT_MARGIN_CM


def parse_usual(raw: str | None) -> tuple[int, int] | None:
    """"5-6Y" -> (5, 6); "6Y" or "6" -> (6, 6); "18-24M" -> (1, 2)."""
    if not raw:
        return None
    text = raw.strip().upper().replace("–", "-").replace(" ", "")
    if m := re.fullmatch(r"(\d{1,2})-(\d{1,2})M", text):
        return max(1, int(m.group(1)) // 12), max(1, int(m.group(2)) // 12)
    if m := re.fullmatch(r"(\d{1,2})-(\d{1,2})(?:Y|YRS?|YEARS?)?", text):
        return int(m.group(1)), int(m.group(2))
    if m := re.fullmatch(r"(?:AGE)?(\d{1,2})(?:Y|YRS?|YEARS?)?", text):
        return int(m.group(1)), int(m.group(1))
    return None


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


def _range_of(label: str) -> tuple[int, int] | None:
    """The ages a sold size covers: "5-6Y" -> (5, 6), "6Y" -> (6, 6)."""
    return parse_usual(label)


def _is_shoe_size(label: str) -> bool:
    return bool(re.fullmatch(r"(?:EU\s*)?\d{2}(?:\.5)?", label.strip().upper()))


def _pick_offered(target: int, offered: list[str], sized_up: bool) -> str | None:
    """The sold size that fits a child needing ``target``.

    Ranges overlap ("5-6Y", "6-7Y"): a child who needs 6 goes into the one that
    STARTS at 6, so there is a year to grow, and the size-up has somewhere to go.
    """
    candidates = []
    for label in offered:
        span = _range_of(label)
        if span and span[0] <= target <= span[1]:
            candidates.append((span, label))
    if not candidates:
        # Nothing covers it: the nearest size above, else the largest there is.
        above = sorted((s, lab) for lab in offered if (s := _range_of(lab)) and s[0] > target)
        if above:
            return above[0][1]
        below = sorted((s, lab) for lab in offered if (s := _range_of(lab)))
        return below[-1][1] if below else None
    # Prefer the range that starts at the target, then the narrowest.
    candidates.sort(key=lambda c: (c[0][0] != target, c[0][1] - c[0][0]))
    return candidates[0][1]


def recommend(age: float | None = None, height_cm: float | None = None,
              chest_cm: float | None = None, usual_size: str | None = None,
              product: dict | None = None) -> dict:
    """One size, from whatever the shopper could tell us.

    ``product`` is an ACTIVE product from the catalogue (title, tags,
    description, options). Without one the answer is a plain "6Y".
    """
    usual = parse_usual(usual_size)
    reasons: list[str] = []
    needs: list[int] = []

    if height_cm:
        needs.append(_size_for(height_cm, "height_cm"))
    if chest_cm:
        needs.append(_size_for(chest_cm, "chest_cm"))
    if usual:
        # They already wear this and it fits; start from its lower end.
        needs.append(usual[0])
    if not needs and age:
        needs.append(int(age))
    if not needs:
        return {"found": False, "still_to_ask": ["age", "height"],
                "tell_customer": "Tell me their age and height and I'll find the right size."}

    target = _clamp(max(needs))
    fit, fit_why = fit_of(product)

    sized_up = False
    near_top = ((height_cm and _near_limit(height_cm, target, "height_cm"))
                or (chest_cm and _near_limit(chest_cm, target, "chest_cm"))
                or (usual and usual[1] > target))
    if fit == "close" and (near_top or not (height_cm or chest_cm)):
        target, sized_up = _clamp(target + 1), True
        reasons.append(f"{fit_why}, so we have sized up.")
    elif fit == "roomy":
        reasons.append(f"{fit_why}, so their usual size will do.")

    offered = [_size_label(v) for v in outfit._options_of(product or {}).get("Size", [])] if product else []
    shoe = bool(offered) and all(_is_shoe_size(s) for s in offered)

    if shoe:
        eu = BY_AGE[target].shoe_eu
        pick = min(offered, key=lambda s: abs(float(re.sub(r"[^\d.]", "", s)) - eu))
        recommended, alternatives = pick, _neighbours(pick, offered)
        reasons = ["Shoe sizes vary by foot length, so this is our best guess for their age."]
    elif offered:
        recommended = _pick_offered(target, offered, sized_up) or f"{target}Y"
        alternatives = _neighbours(recommended, offered)
    else:
        recommended = f"{target}Y"
        alternatives = [f"{a}Y" for a in (target - 1, target, target + 1) if MIN_AGE <= a <= MAX_AGE]

    if not reasons:
        reasons.append("A true-to-size fit with a little room to grow.")

    return {
        "found": True,
        "recommended": recommended,
        "age_label": f"Age {target}",
        "fit": "close" if sized_up else fit,
        "fit_note": " ".join(reasons),
        "sized_up": sized_up,
        "alternatives": alternatives,
        "answers": {
            "age": f"{int(age)} years" if age else None,
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
