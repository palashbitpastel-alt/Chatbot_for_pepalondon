"""The assistant's look and behaviour, set per shop in the app's admin page.

One list, ``FIELDS``, describes every setting: the admin page draws its form
from it and the storefront widget reads the saved values through it, so the
two can never disagree. Values are checked against it on the way in - a
colour is a colour, a number stays in range, a choice is one of the choices -
so nothing a merchant types reaches the storefront unchecked.

Colours marked optional default to empty, which means "follow the main
colours". Every other default is the original Pepa design, so a shop that has
saved nothing looks exactly as designed.
"""

import re
from typing import Any

# Google Fonts offered for the chat (body and headings). "default" is the
# design's own: DM Sans for text, TT Drugs (or Helvetica Neue) for headings.
FONTS = ["default", "Inter", "Lato", "Montserrat", "Nunito", "Open Sans", "Poppins", "Raleway", "Roboto",
         "Work Sans", "DM Serif Display", "Cormorant Garamond", "EB Garamond", "Libre Baskerville",
         "Playfair Display", "Lora"]


def _f(id_: str, type_: str, label: str, group: str, default: Any = None, **extra: Any) -> dict:
    return {"id": id_, "type": type_, "label": label, "group": group, "default": default, **extra}


FIELDS: list[dict] = [
    # Assistant: what it says about the shop (read by the AI, not shown as settings in the chat)
    _f("store_name", "text", "Store name in greetings", "Assistant", "", ai=True,
       info="Empty uses your Shopify store name."),
    _f("store_description", "text", "What the store sells", "Assistant", "", ai=True,
       info="One line the assistant uses to describe your shop, e.g. \"clothes and shoes for babies and children\"."),
    _f("welcome_message", "textarea", "Welcome message when the chat opens", "Assistant", "", ai=True,
       info="Empty uses a short welcome with your store name."),

    # Texts
    _f("launcher_label", "text", "Launcher label", "Texts", "Ask Pepa"),
    _f("wordmark", "text", "Wordmark", "Texts", "Pepa London"),
    _f("assistant_name", "text", "Assistant name", "Texts", "Pepa Assistant"),
    _f("welcome_title", "text", "Welcome heading", "Texts", "What are we shopping for today?"),
    _f("starters", "textarea", "Try asking", "Texts",
       "size | Find the right size\ncompare | Which dress would suit a wedding?\ncompare | What should he wear to a wedding?",
       info="One per line as icon | text. Icons: search, size, look, compare, truck, sparkle."),
    _f("placeholder", "text", "Input placeholder", "Texts", "Ask a follow-up…"),
    _f("shopper_note", "text", "Sidebar note", "Texts", "Sizes, occasions and budgets, understood in your own words."),
    _f("legal", "text", "Small print under the input", "Texts",
       "Pepa Assistant can make mistakes - please check sizes and delivery dates."),

    # Colours
    _f("accent", "color", "Accent", "Colours", "#6f2c2c", info="Prices, highlights and totals."),
    _f("background", "color", "Background", "Colours", "#fbf8f4"),
    _f("panel", "color", "Cards and panels", "Colours", "#ffffff"),
    _f("text", "color", "Text", "Colours", "#241e1b"),
    _f("lines", "color", "Lines and borders", "Colours", "#e0d7cb"),
    _f("button", "color", "Buttons", "Colours", "#241e1b"),
    _f("button_text", "color", "Button text", "Colours", "#ffffff"),

    # Fonts and corners
    _f("body_font", "select", "Body font", "Fonts and corners", "default", options=FONTS),
    _f("heading_font", "select", "Heading font", "Fonts and corners", "default", options=FONTS),
    _f("heading_font_url", "url", "Heading font file (.woff2)", "Fonts and corners", "",
       info="Optional: your own heading font file, used when Heading font is set to default."),
    _f("corner_radius", "range", "Corner rounding", "Fonts and corners", 2, min=0, max=16, unit="px"),
    _f("button_radius", "range", "Button corner rounding", "Fonts and corners", 2, min=0, max=24, unit="px"),

    # Launcher
    _f("position", "select", "Position", "Launcher", "right", options=["right", "left"]),
    _f("launcher_style", "select", "Style", "Launcher", "label", options=["label", "icon"]),
    _f("launcher_icon", "select", "Icon", "Launcher", "sparkle", options=["sparkle", "chat", "bag", "search"]),
    _f("launcher_bottom", "range", "Distance from bottom", "Launcher", 24, min=0, max=120, unit="px"),
    _f("launcher_side", "range", "Distance from side", "Launcher", 24, min=0, max=120, unit="px"),
    _f("launcher_bg", "color", "Background", "Launcher", None, optional=True, info="Empty follows Buttons."),
    _f("launcher_text", "color", "Text", "Launcher", None, optional=True),
    _f("launcher_radius", "range", "Corner rounding", "Launcher", 40, min=0, max=40, unit="px"),
    _f("open_on_load", "checkbox", "Open once per visit automatically", "Launcher", False),

    # Messages and chips
    _f("user_bg", "color", "Shopper's message background", "Messages and chips", None, optional=True,
       info="Empty follows Buttons."),
    _f("user_text", "color", "Shopper's message text", "Messages and chips", None, optional=True),
    _f("chip_bg", "color", "Chip background", "Messages and chips", None, optional=True),
    _f("chip_text", "color", "Chip text", "Messages and chips", None, optional=True),
    _f("chip_border", "color", "Chip border", "Messages and chips", None, optional=True),
    _f("chip_active_bg", "color", "Selected chip background", "Messages and chips", None, optional=True),
    _f("chip_active_text", "color", "Selected chip text", "Messages and chips", None, optional=True),
    _f("chip_active_border", "color", "Selected chip border", "Messages and chips", None, optional=True),
    _f("chip_radius", "range", "Chip corner rounding", "Messages and chips", 2, min=0, max=24, unit="px"),

    # Variant options
    _f("variant_bg", "color", "Background", "Variant options", None, optional=True),
    _f("variant_text", "color", "Text", "Variant options", None, optional=True),
    _f("variant_border", "color", "Border", "Variant options", None, optional=True),
    _f("variant_hover_border", "color", "Border on hover", "Variant options", None, optional=True),
    _f("variant_active_bg", "color", "Selected background", "Variant options", None, optional=True,
       info="Empty follows Buttons."),
    _f("variant_active_text", "color", "Selected text", "Variant options", None, optional=True),
    _f("variant_active_border", "color", "Selected border", "Variant options", None, optional=True),
    _f("variant_radius", "range", "Corner rounding", "Variant options", 0, min=0, max=24, unit="px"),

    # Product cards
    _f("card_title", "color", "Product name", "Product cards", None, optional=True),
    _f("card_price", "color", "Price", "Product cards", None, optional=True),
    _f("card_image_radius", "range", "Image corner rounding", "Product cards", 0, min=0, max=24, unit="px"),
    _f("tile_radius", "range", "Category tile corner rounding", "Product cards", 0, min=0, max=24, unit="px"),
    _f("add_bg", "color", "Add to bag background", "Product cards", None, optional=True),
    _f("add_text", "color", "Add to bag text", "Product cards", None, optional=True),
    _f("add_border", "color", "Add to bag border", "Product cards", None, optional=True),
    _f("add_hover_bg", "color", "Add to bag background on hover", "Product cards", None, optional=True,
       info="Empty follows Buttons."),
    _f("add_hover_text", "color", "Add to bag text on hover", "Product cards", None, optional=True),
    _f("add_radius", "range", "Add to bag corner rounding", "Product cards", 0, min=0, max=24, unit="px"),
    _f("input_bg", "color", "Message box background", "Product cards", None, optional=True),
    _f("input_border", "color", "Message box border", "Product cards", None, optional=True),

    # Where it shows
    _f("show_on_desktop", "checkbox", "Show the launcher on desktop", "Where it shows", True),
    _f("show_on_mobile", "checkbox", "Show the launcher on mobile", "Where it shows", True),
    _f("hide_on_cart", "checkbox", "Hide on the cart page", "Where it shows", False),
    _f("hide_on_account", "checkbox", "Hide on customer account pages", "Where it shows", False),
    _f("hide_on_search", "checkbox", "Hide on the search results page", "Where it shows", False),

    # Teaser
    _f("teaser_enabled", "checkbox", "Show a teaser beside the launcher", "Teaser message", False,
       info="Once per visit, until the shopper opens the chat or closes it."),
    _f("teaser_text", "text", "Teaser text", "Teaser message", "Need help finding the perfect outfit?"),
    _f("teaser_delay", "range", "Show after", "Teaser message", 5, min=0, max=60, unit="s"),

    # Features
    _f("show_recent", "checkbox", "Recent chats", "Features", True),
    _f("show_saved", "checkbox", "Saved pieces", "Features", True),
    _f("show_size_finder", "checkbox", "Size finder", "Features", True),
    _f("show_understood", "checkbox", "Understood panel", "Features", True),
    _f("show_bag", "checkbox", "Bag panel", "Features", True),
    _f("show_note", "checkbox", "Sidebar note", "Features", True),

    # Talk to our team
    _f("help_label", "text", "Heading", "Talk to our team", "Talk to our team"),
    _f("help_email", "text", "Email address", "Talk to our team", ""),
    _f("help_whatsapp", "text", "WhatsApp number", "Talk to our team", "",
       info="With country code, e.g. +44 7700 900123."),
    _f("help_url", "url", "Contact page", "Talk to our team", ""),
]

_BY_ID = {f["id"]: f for f in FIELDS}
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
_TEXT_LIMIT = {"text": 300, "textarea": 2000, "url": 500}


def defaults() -> dict:
    return {f["id"]: f["default"] for f in FIELDS}


def clean(values: dict) -> dict:
    """Only known settings, each checked against its field. Bad values are dropped."""
    out: dict = {}
    for key, value in (values or {}).items():
        field = _BY_ID.get(key)
        if field is None:
            continue
        kind = field["type"]
        if kind == "color":
            if field.get("optional") and value in (None, ""):
                out[key] = None
            elif isinstance(value, str) and _COLOUR.match(value):
                out[key] = value.lower()
        elif kind == "checkbox":
            out[key] = bool(value)
        elif kind == "range":
            try:
                out[key] = max(field["min"], min(field["max"], int(value)))
            except (TypeError, ValueError):
                pass
        elif kind == "select":
            if value in field["options"]:
                out[key] = value
        elif kind in _TEXT_LIMIT:
            text = str(value or "")[:_TEXT_LIMIT[kind]]
            if kind == "url" and text and not text.startswith("https://"):
                continue
            out[key] = text
    return out


def for_shop(saved: dict | None) -> dict:
    """Every setting for a shop: its saved values over the defaults."""
    return {**defaults(), **clean(saved or {})}


def for_storefront(saved: dict | None) -> dict:
    """What the widget needs - the AI-only fields stay on the server."""
    return {k: v for k, v in for_shop(saved).items() if not _BY_ID[k].get("ai")}
