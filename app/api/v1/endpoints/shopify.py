import html
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.config import settings
from app.services import installs, insights, shopify_search, shops, widget_settings
from app.services.shopify_store import fetch_store_snapshot, is_configured

logger = logging.getLogger(__name__)

router = APIRouter(tags=["shopify"])


@router.get("/shopify/status")
async def shopify_status() -> dict:
    """Connection state plus what the store's live data says (currency, granted
    scopes, where campaign data comes from). Nothing here is cached - it's a
    fresh read on every call, same as the rest of the dashboard."""
    snapshot = await fetch_store_snapshot()
    return {
        "configured": is_configured(),
        "store_url": settings.SHOPIFY_STORE_URL or None,
        "api_version": settings.SHOPIFY_API_VERSION,
        **insights.store_meta(snapshot),
        # Whether product search runs on Shopify's own storefront search.
        "storefront_search": await shopify_search.product_ids("a", 1) is not None,
    }


# ── The app inside the merchant's Shopify admin ────────────────────────────
# Shopify opens this page (the app's application_url) right after an install,
# and whenever the merchant opens the app, with an id_token naming the shop.
# Verifying it and saving the shop is the whole install: nothing to add by hand.

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="shopify-api-key" content="{api_key}">
<script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"></script>
<title>Pepa Assistant</title>
<style>
  body {{ margin: 0; font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #303030; background: #f1f1f1; }}
  main {{ max-width: 640px; margin: 32px auto; padding: 0 16px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px 24px; box-shadow: 0 1px 2px rgba(0,0,0,.08); }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }}
  p {{ margin: 0 0 12px; }}
  ol {{ margin: 0 0 16px; padding-left: 20px; }}
  a.btn {{ display: inline-block; background: #303030; color: #fff; padding: 9px 16px; border-radius: 8px; text-decoration: none; font-weight: 600; }}
  .muted {{ color: #616161; font-size: 13px; }}
  .bad {{ color: #8e1f0b; }}
</style></head>
<body><main><div class="card">{body}</div></main></body></html>"""


def _page(body: str, shop: str | None = None, status: int = 200) -> HTMLResponse:
    # Shopify shows the app in an iframe on admin.shopify.com.
    ancestors = "https://admin.shopify.com" + (f" https://{shop}" if shop else "")
    return HTMLResponse(_PAGE.format(api_key=html.escape(settings.SHOPIFY_CLIENT_ID), body=body), status_code=status,
                        headers={"Content-Security-Policy": f"frame-ancestors {ancestors};"})


@router.get("/shopify/app", response_class=HTMLResponse, include_in_schema=False)
async def shopify_app(request: Request) -> HTMLResponse:
    id_token = request.query_params.get("id_token")
    if not id_token:
        return _page("<h1>Pepa Assistant</h1><p>Open the app from your Shopify admin (Apps &gt; Pepa AI Chatbot).</p>")
    try:
        shop = await installs.install(id_token)
    except installs.InstallError as exc:
        logger.warning("Install could not be completed: %s", exc)
        return _page(f'<h1>Pepa Assistant</h1><p class="bad">{html.escape(str(exc))}</p>', status=400)
    editor = (f"https://{shop}/admin/themes/current/editor?context=apps"
              f"&activateAppId={settings.SHOPIFY_CLIENT_ID}/pepa-assistant")
    values = widget_settings.for_shop(installs.settings_for(shop))
    return _settings_page(shop, editor, values)


# ── Webhooks: uninstall, and the privacy requests every app must answer ─────

@router.post("/shopify/webhooks", include_in_schema=False)
async def shopify_webhooks(request: Request) -> JSONResponse:
    body = await request.body()
    if not installs.webhook_is_genuine(body, request.headers.get("x-shopify-hmac-sha256")):
        return JSONResponse({"detail": "Invalid signature"}, status_code=401)
    topic = (request.headers.get("x-shopify-topic") or "").lower()
    shop = request.headers.get("x-shopify-shop-domain") or ""
    if topic == "app/uninstalled":
        await installs.uninstall(shop)
    elif topic == "shop/redact":
        await installs.forget(shop)
    # customers/data_request and customers/redact: chats are kept under random
    # session ids with no customer link, so there is nothing tied to a customer
    # to send or delete; Shopify needs the 200.
    logger.info("Webhook %s from %s", topic, shop)
    return JSONResponse({"ok": True})


# The settings page, in Polaris web components so it looks and behaves like
# the rest of the Shopify admin (Settings template): one section per group,
# Shopify's save bar on any change, Save in the admin title bar. It is drawn
# here with the shop's saved values; saving goes to /shopify/app/settings,
# signed with a fresh App Bridge session token.

_GROUP_HELP = {
    "Assistant": "What the assistant says about your shop.",
    "Texts": "The words shoppers see in the chat.",
    "Colours": "The main colours. The finer parts below follow these unless you set them.",
    "Fonts and corners": "Typefaces and how rounded the chat looks.",
    "Launcher": "The button that opens the chat.",
    "Messages and chips": "The shopper's own messages and the suggestion chips. Leave a colour empty to follow the main colours.",
    "Variant options": "Size and colour choices on products. Leave a colour empty to follow the main colours.",
    "Product cards": "Product cards, the add to bag button and the message box.",
    "Where it shows": "Pages and devices the assistant appears on.",
    "Teaser message": "A short line beside the launcher to invite a first question.",
    "Features": "Parts of the chat you can turn off.",
    "Talk to our team": "Ways to reach a person, shown in the chat's sidebar.",
}
_GRID_GROUPS = {"Colours", "Fonts and corners", "Launcher", "Messages and chips", "Variant options", "Product cards"}
_OPTION_LABELS = {"right": "Bottom right", "left": "Bottom left", "label": "Icon and text", "icon": "Icon only",
                  "sparkle": "Sparkle", "chat": "Chat bubble", "bag": "Shopping bag", "search": "Search",
                  "default": "Pepa's own"}


def _attr(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _field(f: dict, value) -> str:
    common = f'label="{_attr(f["label"])}" name="{_attr(f["id"])}"'
    if f.get("info"):
        common += f' details="{_attr(f["info"])}"'
    kind = f["type"]
    if kind == "checkbox":
        return f'<s-switch {common}{" checked" if value else ""}></s-switch>'
    if kind == "select":
        options = "".join(f'<s-option value="{_attr(o)}"{" selected" if o == value else ""}>{html.escape(_OPTION_LABELS.get(o, o))}</s-option>'
                          for o in f["options"])
        return f'<s-select {common} value="{_attr(value)}">{options}</s-select>'
    if kind == "range":
        unit = f' suffix="{_attr(f.get("unit", ""))}"' if f.get("unit") else ""
        return f'<s-number-field {common} value="{_attr(value)}" min="{f["min"]}" max="{f["max"]}" step="1"{unit}></s-number-field>'
    if kind == "color":
        empty = ' placeholder="Follows the main colours"' if f.get("optional") else ""
        return f'<s-color-field {common} value="{_attr(value or "")}"{empty}></s-color-field>'
    if kind == "textarea":
        return f'<s-text-area {common} rows="4" value="{_attr(value)}"></s-text-area>'
    if kind == "url":
        return f'<s-url-field {common} value="{_attr(value)}"></s-url-field>'
    return f'<s-text-field {common} value="{_attr(value)}"></s-text-field>'


def _settings_page(shop: str, editor: str, values: dict) -> HTMLResponse:
    groups: list[str] = list(dict.fromkeys(f["group"] for f in widget_settings.FIELDS))
    sections = []
    for group in groups:
        fields = "".join(_field(f, values.get(f["id"])) for f in widget_settings.FIELDS if f["group"] == group)
        layout = (f'<s-grid gridTemplateColumns="repeat(auto-fill, minmax(220px, 1fr))" gap="base">{fields}</s-grid>'
                  if group in _GRID_GROUPS else f'<s-stack gap="base">{fields}</s-stack>')
        sections.append(f'<s-section heading="{_attr(group)}"><s-stack gap="base">'
                        f'<s-paragraph color="subdued">{html.escape(_GROUP_HELP.get(group, ""))}</s-paragraph>'
                        f'{layout}</s-stack></s-section>')
    types = {f["id"]: f["type"] for f in widget_settings.FIELDS}
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="shopify-api-key" content="{_attr(settings.SHOPIFY_CLIENT_ID)}">
<script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"></script>
<script src="https://cdn.shopify.com/shopifycloud/polaris.js"></script>
<title>Pepa Assistant</title>
</head><body>
<s-page heading="Pepa Assistant" inlineSize="base">
  <s-button slot="primary-action" variant="primary" id="save-top">Save</s-button>
  <s-stack gap="base">
    <s-banner heading="Switch the assistant on in your theme" tone="info">
      <s-stack gap="small-200">
        <s-paragraph>Connected to {html.escape(shop)}. Turn on Pepa Assistant under App embeds once; everything else is set on this page.</s-paragraph>
        <s-stack direction="inline"><s-button href="{_attr(editor)}" target="_top">Open the theme editor</s-button></s-stack>
      </s-stack>
    </s-banner>
    <form id="settings" data-save-bar data-discard-confirmation>
      <s-stack gap="base">{''.join(sections)}</s-stack>
    </form>
    <s-paragraph color="subdued">Changes show on your store the next time a page loads.</s-paragraph>
  </s-stack>
</s-page>
<script>
const TYPES = {json.dumps(types)};
const form = document.getElementById('settings');
document.getElementById('save-top').addEventListener('click', () => form.requestSubmit());
function read() {{
  const values = {{}};
  form.querySelectorAll('[name]').forEach((el) => {{
    const kind = TYPES[el.getAttribute('name')];
    if (!kind) return;
    if (kind === 'checkbox') values[el.getAttribute('name')] = !!el.checked;
    else if (kind === 'range') values[el.getAttribute('name')] = Number(el.value);
    else if (kind === 'color') values[el.getAttribute('name')] = el.value ? String(el.value).slice(0, 7) : null;
    else values[el.getAttribute('name')] = el.value || '';
  }});
  return values;
}}
form.addEventListener('submit', async (event) => {{
  event.preventDefault();
  try {{
    const token = await shopify.idToken();
    const res = await fetch('app/settings', {{ method: 'POST', headers: {{ 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' }}, body: JSON.stringify(read()) }});
    if (!res.ok) throw new Error((await res.json().catch(() => ({{}}))).detail || res.statusText);
    shopify.toast.show('Settings saved');
  }} catch (error) {{
    shopify.toast.show('Could not save: ' + error.message, {{ isError: true }});
  }}
}});
</script>
</body></html>"""
    return HTMLResponse(page, headers={"Content-Security-Policy": f"frame-ancestors https://admin.shopify.com https://{shop};"})


def _shop_from_bearer(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise installs.InstallError("Sign in through your Shopify admin.")
    return installs.verify_id_token(auth[7:].strip())


@router.get("/shopify/app/settings", include_in_schema=False)
async def get_app_settings(request: Request) -> JSONResponse:
    try:
        shop = _shop_from_bearer(request)
    except installs.InstallError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    return JSONResponse({"shop": shop, "settings": widget_settings.for_shop(installs.settings_for(shop))})


@router.post("/shopify/app/settings", include_in_schema=False)
async def save_app_settings(request: Request) -> JSONResponse:
    try:
        shop = _shop_from_bearer(request)
    except installs.InstallError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        values = await request.json()
    except ValueError:
        return JSONResponse({"detail": "Not JSON"}, status_code=400)
    saved = await installs.save_settings(shop, values if isinstance(values, dict) else {})
    logger.info("Settings saved for %s", shop)
    return JSONResponse({"shop": shop, "settings": widget_settings.for_shop(saved)})


# The storefront widget reads its shop's settings here (the shop comes from
# ?shop=, checked by ShopMiddleware like every storefront call).
@router.get("/support/widget-config")
async def widget_config() -> JSONResponse:
    return JSONResponse({"settings": widget_settings.for_storefront(installs.settings_for(shops.current()))},
                        headers={"Cache-Control": "public, max-age=60"})

