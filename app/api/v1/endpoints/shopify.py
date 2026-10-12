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
        return _sign_in_page()
    try:
        shop = await installs.install(id_token)
    except installs.InstallError as exc:
        logger.warning("Install could not be completed: %s", exc)
        return _page(f'<h1>Pepa Assistant</h1><p class="bad">{html.escape(str(exc))}</p>', status=400)
    editor = (f"https://{shop}/admin/themes/current/editor?context=apps"
              f"&activateAppId={settings.SHOPIFY_CLIENT_ID}/pepa-assistant")
    values = widget_settings.for_shop(installs.settings_for(shop))
    from app.services import billing
    return _home_page(shop, editor, values, await billing.for_merchant(shop))


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


def _nice_date(iso: str) -> str:
    from datetime import date
    try:
        return date.fromisoformat(iso).strftime("%-d %b %Y")
    except ValueError:
        return iso


def _money(amount: float) -> str:
    return f"${amount:,.2f}".replace(".00", "")


def _time_left(days: int | None) -> str:
    """'2 months, 10 days left', '12 days left', 'Expired'."""
    if days is None:
        return ""
    if days < 0:
        return "Expired"
    if days == 0:
        return "Ends today"
    if days < 60:
        return f"{days} day{'s' if days != 1 else ''} left"
    months, rest = divmod(days, 30)
    return f"{months} months" + (f", {rest} day{'s' if rest != 1 else ''}" if rest else "") + " left"


def _time_bar(info: dict) -> str:
    days, total = info.get("days_left"), info.get("days_total")
    if days is None or not total or days < 0:
        return ""
    pct = max(0, min(100, round(days * 100 / total)))
    return (f'<div class="pa-meter" role="img" aria-label="{pct}% of the paid period left">'
            f'<div style="width:{pct}%"{" class=hot" if days <= 7 else ""}></div></div>')


def _period_label(q: dict) -> str:
    months, off = int(q["months"]), int(q.get("discount") or 0)
    text = "1 month" if months == 1 else f"{months} months"
    return text + (f" · save {off}%" if off else "")


def _asked_heading(name: str, req: dict) -> str:
    months = int(req.get("months") or 1)
    total = req.get("total")
    period = "1 month" if months == 1 else f"{months} months"
    return f"You asked for the {name} plan for {period}" + (f" ({_money(float(total))})" if total else "")


def _plan_sections(shop: str, info: dict) -> str:
    """The subscription part of the app's page: current plan, usage, paid-until,
    how to pay, and the plans to move to."""
    plan = info.get("plan")
    used, limit, pct = info["chats_used"], info.get("chat_limit"), info.get("percent")
    pay = info.get("payment_url") or ""
    contact = settings.SUPPORT_CONTACT_EMAIL
    days = info.get("days_left")
    banners = []
    if not info["active"]:
        banners.append(("critical", "The assistant is paused on your store",
                        "Shoppers can't chat until your plan is renewed. Renew below or contact us."))
    elif days is not None and days < 0:
        banners.append(("critical", "Your plan has expired", "Renew to keep the assistant running."))
    elif days is not None and days <= 7:
        banners.append(("warning", f"Your plan renews in {days} day{'s' if days != 1 else ''}",
                        "Pay before then to keep the assistant running without a break."))
    if info["active"] and pct is not None and pct >= 100:
        banners.append(("critical", "You have used all of this month's chats",
                        "New conversations are paused until next month. Upgrade to continue now."))
    elif info["active"] and pct is not None and pct >= 80:
        banners.append(("warning", f"You have used {pct}% of this month's chats",
                        "Upgrade before you reach the limit so shoppers can keep chatting."))
    banner_html = "".join(
        f'<s-banner heading="{_attr(h)}" tone="{tone}"><s-paragraph>{html.escape(t)}</s-paragraph></s-banner>'
        for tone, h, t in banners)
    status = ('<s-badge tone="success">Active</s-badge>' if info["active"]
              else '<s-badge tone="critical">Paused</s-badge>')
    plan_box = (f'<s-text color="subdued">Current plan</s-text>'
                f'<s-stack direction="inline" gap="small-200" alignItems="center"><s-heading>{html.escape(plan["name"] if plan else "No plan yet")}</s-heading>{status}</s-stack>'
                f'<s-text>{html.escape(plan["price"]) if plan else "Choose a plan below"}</s-text>')
    usage = f"{used:,} of {limit:,}" if limit else f"{used:,}"
    bar = (f'<div class="pa-meter" role="img" aria-label="{pct}% used"><div style="width:{pct}%"'
           f'{" class=hot" if pct >= 80 else ""}></div></div>') if pct is not None else '<s-text color="subdued">No monthly limit</s-text>'
    paid = (f'<s-heading>{html.escape(_time_left(days))}</s-heading>'
            f'{_time_bar(info)}'
            f'<s-text color="subdued">{html.escape(("Ended " if days is not None and days < 0 else "Until ") + _nice_date(info["paid_until"]))}</s-text>'
            ) if info.get("paid_until") else '<s-heading>–</s-heading><s-text color="subdued">Not set yet</s-text>'
    box = lambda inner: f'<s-box padding="base" border="base" borderRadius="base"><s-stack gap="small-200">{inner}</s-stack></s-box>'
    actions = []
    if pay:
        actions.append(f'<s-button variant="primary" href="{_attr(pay)}" target="_blank">Pay or renew</s-button>')
    if contact:
        actions.append(f'<s-button href="mailto:{_attr(contact)}?subject={_attr("Pepa Assistant - " + shop)}">Contact support</s-button>')
    usage_section = (
        '<s-section heading="Plan and usage"><s-stack gap="base">' + banner_html +
        '<s-grid gridTemplateColumns="repeat(auto-fit, minmax(200px, 1fr))" gap="base">' +
        box(plan_box) +
        box(f'<s-text color="subdued">Chats this month</s-text><s-heading>{usage}</s-heading>{bar}') +
        box(f'<s-text color="subdued">Time left</s-text>{paid}') +
        '</s-grid>' +
        (f'<s-stack direction="inline" gap="base">{"".join(actions)}</s-stack>' if actions else '') +
        '</s-stack></s-section>')
    requested = (info.get("requested") or {}).get("plan")
    if requested:
        asked = next((p for p in info.get("plans") or [] if p.get("key") == requested), {})
        usage_section = usage_section.replace(
            '<s-section heading="Plan and usage"><s-stack gap="base">',
            '<s-section heading="Plan and usage"><s-stack gap="base">'
            f'<s-banner heading="{_attr(_asked_heading(asked.get("name", requested), info.get("requested") or {}))}" tone="info">'
            f'<s-paragraph>{html.escape("We will confirm it and send you a payment link. Your plan changes once it is paid.")}</s-paragraph></s-banner>', 1)
    cards = []
    for p in info.get("plans") or []:
        current = plan is not None and p.get("key") == plan.get("key")
        better = plan is None or (p.get("chats") or 0) > (plan.get("chats") or 0)
        label = "Upgrade" if better and plan else "Choose" if not plan else "Switch"
        if p.get("key") == requested:
            button = '<s-badge tone="warning">Requested</s-badge>'
        else:
            button = ("" if current else
                      f'<s-button data-plan="{_attr(p.get("key"))}" data-pay="{_attr(p.get("payment_url") or pay or "")}"'
                      f'{" variant=\"primary\"" if better else ""}>{label}</s-button>')
        features = "".join(f"<s-list-item>{html.escape(str(f))}</s-list-item>" for f in p.get("features") or [])
        from app.services import billing as _billing
        quotes = {str(q["months"]): _billing.quote(p, q["months"]) for q in info.get("periods") or []} if p.get("monthly") else {}
        pays = {str(k): v for k, v in (p.get("payment_urls") or {}).items()} if isinstance(p.get("payment_urls"), dict) else {}
        button = button.replace("<s-button data-plan=", f"<s-button data-pays=\"{_attr(json.dumps(pays))}\" data-plan=", 1)
        cards.append(box(
            f'<s-stack direction="inline" gap="small-200" alignItems="center"><s-heading>{html.escape(str(p.get("name")))}</s-heading>'
            f'{"<s-badge tone=\"info\">Current plan</s-badge>" if current else ""}</s-stack>'
            f'<s-text type="strong" data-price="{_attr(p.get("key"))}" data-quotes="{_attr(json.dumps(quotes))}">{html.escape(str(p.get("price") or ""))}</s-text>'
            f'<s-text color="subdued" data-total="{_attr(p.get("key"))}">Billed monthly</s-text>'
            f'<s-unordered-list>{features}</s-unordered-list>{button}'))
    periods = info.get("periods") or []
    options = "".join(
        f'<s-option value="{q["months"]}"{" selected" if q["months"] == 1 else ""}>'
        f'{html.escape(_period_label(q))}</s-option>'
        for q in periods)
    chooser = (f'<s-select label="Billing period" id="pa-period" value="1">{options}</s-select>' if len(periods) > 1 else "")
    plans_section = ('<s-section heading="Plans"><s-stack gap="base">' + chooser
                     + '<s-grid gridTemplateColumns="repeat(auto-fit, minmax(220px, 1fr))" gap="base">'
                     + "".join(cards) + '</s-grid></s-stack></s-section>') if cards else ""
    return usage_section + plans_section


APP_BASE = "/api/v1/shopify/app"
# The app's pages in the Shopify admin sidebar (s-app-nav), with the setting
# groups each one holds. The home page is the app's own name in the sidebar.
_APP_PAGES = [
    ("content", "Content", ["Assistant", "Texts"]),
    ("appearance", "Appearance", ["Colours", "Fonts and corners", "Launcher", "Messages and chips",
                                  "Variant options", "Product cards"]),
    ("behaviour", "Behaviour", ["Where it shows", "Teaser message", "Features", "Talk to our team"]),
    ("plan", "Plan", None),
]
_PAGE_HELP = {
    "content": "What the assistant says: your store's name and description, and the words shoppers see.",
    "appearance": "How the assistant looks: colours, fonts, the launcher, chips and product cards.",
    "behaviour": "Where the assistant shows, the teaser, which parts are on, and how to reach your team.",
    "plan": "Your plan, this month's chats, and how to pay or change plan.",
}


def _shell(shop: str, heading: str, body: str, script: str = "", save: bool = False) -> HTMLResponse:
    """One app page: App Bridge, Polaris, the sidebar menu, a heading."""
    nav = "".join(f'<s-link href="{APP_BASE}/{key}">{html.escape(label)}</s-link>' for key, label, _ in _APP_PAGES)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="shopify-api-key" content="{_attr(settings.SHOPIFY_CLIENT_ID)}">
<script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"></script>
<script src="https://cdn.shopify.com/shopifycloud/polaris.js"></script>
<title>Pepa Assistant</title>
<style>
  .pa-meter {{ height: 8px; border-radius: 4px; background: #e3e3e3; overflow: hidden; }}
  .pa-meter > div {{ height: 100%; background: #29845a; border-radius: 4px; }}
  .pa-meter > div.hot {{ background: #b28400; }}
</style>
</head><body>
<s-app-nav><s-link href="{APP_BASE}" rel="home">Home</s-link>{nav}</s-app-nav>
<s-page heading="{_attr(heading)}" inlineSize="base">
  {'<s-button slot="primary-action" variant="primary" id="save-top">Save</s-button>' if save else ''}
  <s-stack gap="base">{body}</s-stack>
</s-page>
{script}
</body></html>"""
    return HTMLResponse(page, headers={"Content-Security-Policy": f"frame-ancestors https://admin.shopify.com https://{shop};"})


def _home_page(shop: str, editor: str, values: dict, info: dict) -> HTMLResponse:
    plan = info.get("plan")
    used, limit = info["chats_used"], info.get("chat_limit")
    status = ('<s-badge tone="success">Active</s-badge>' if info["active"]
              else '<s-badge tone="critical">Paused</s-badge>')
    box = lambda inner: f'<s-box padding="base" border="base" borderRadius="base"><s-stack gap="small-200">{inner}</s-stack></s-box>'
    cards = (
        '<s-grid gridTemplateColumns="repeat(auto-fit, minmax(200px, 1fr))" gap="base">'
        + box(f'<s-text color="subdued">Assistant</s-text><s-stack direction="inline" gap="small-200" alignItems="center"><s-heading>{html.escape(values.get("assistant_name") or "Assistant")}</s-heading>{status}</s-stack>')
        + box(f'<s-text color="subdued">Chats this month</s-text><s-heading>{used:,}{f" of {limit:,}" if limit else ""}</s-heading>')
        + box(f'<s-text color="subdued">Plan</s-text><s-heading>{html.escape(plan["name"] if plan else "No plan yet")}</s-heading>'
              + (f'<s-text>{html.escape(_time_left(info.get("days_left")))}</s-text>{_time_bar(info)}' if info.get("paid_until") else "")
              + f'<s-link href="{APP_BASE}/plan">See plan and usage</s-link>')
        + '</s-grid>')
    links = "".join(
        box(f'<s-heading>{html.escape(label)}</s-heading><s-paragraph color="subdued">{html.escape(_PAGE_HELP[key])}</s-paragraph>'
            f'<s-link href="{APP_BASE}/{key}">Open {html.escape(label.lower())}</s-link>')
        for key, label, _ in _APP_PAGES)
    body = (
        cards
        + f'<s-banner heading="Switch the assistant on in your theme" tone="info"><s-stack gap="small-200">'
          f'<s-paragraph>Connected to {html.escape(shop)}. Turn on Pepa Assistant under App embeds once; set everything else here in the app.</s-paragraph>'
          f'<s-stack direction="inline"><s-button href="{_attr(editor)}" target="_top">Open the theme editor</s-button></s-stack></s-stack></s-banner>'
        + f'<s-section heading="Set up your assistant"><s-grid gridTemplateColumns="repeat(auto-fit, minmax(220px, 1fr))" gap="base">{links}</s-grid></s-section>')
    return _shell(shop, values.get("assistant_name") or "Pepa Assistant", body)


def _form_page(shop: str, key: str, title: str, groups: list[str], values: dict) -> HTMLResponse:
    fields_in = [f for f in widget_settings.FIELDS if f["group"] in groups]
    sections = []
    for group in groups:
        fields = "".join(_field(f, values.get(f["id"])) for f in fields_in if f["group"] == group)
        layout = (f'<s-grid gridTemplateColumns="repeat(auto-fill, minmax(220px, 1fr))" gap="base">{fields}</s-grid>'
                  if group in _GRID_GROUPS else f'<s-stack gap="base">{fields}</s-stack>')
        sections.append(f'<s-section heading="{_attr(group)}"><s-stack gap="base">'
                        f'<s-paragraph color="subdued">{html.escape(_GROUP_HELP.get(group, ""))}</s-paragraph>'
                        f'{layout}</s-stack></s-section>')
    types = {f["id"]: f["type"] for f in fields_in}
    body = (f'<s-paragraph color="subdued">{html.escape(_PAGE_HELP[key])} Changes show on your store the next time a page loads.</s-paragraph>'
            f'<form id="settings" data-save-bar data-discard-confirmation><s-stack gap="base">{"".join(sections)}</s-stack></form>')
    script = f"""<script>
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
    const res = await fetch('{APP_BASE}/settings', {{ method: 'POST', headers: {{ 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' }}, body: JSON.stringify(read()) }});
    if (!res.ok) throw new Error((await res.json().catch(() => ({{}}))).detail || res.statusText);
    shopify.toast.show('Settings saved');
  }} catch (error) {{
    shopify.toast.show('Could not save: ' + error.message, {{ isError: true }});
  }}
}});
</script>"""
    return _shell(shop, title, body, script, save=True)


# A plan button: tell us which plan they want, open its payment link if there is one.
_PLAN_SCRIPT = f"""<script>
const periodSelect = document.getElementById('pa-period');
const months = () => Number((periodSelect && periodSelect.value) || 1);
const money = (n) => '$' + Number(n).toLocaleString('en-US', {{ minimumFractionDigits: 0, maximumFractionDigits: 2 }});
function showPrices() {{
  const m = String(months());
  document.querySelectorAll('[data-price]').forEach((el) => {{
    const q = JSON.parse(el.dataset.quotes || '{{}}')[m];
    if (!q) return;
    el.textContent = money(q.per_month) + ' / month';
    const total = document.querySelector('[data-total="' + el.dataset.price + '"]');
    if (total) total.textContent = m === '1' ? 'Billed monthly' : money(q.total) + ' every ' + m + ' months' + (q.discount ? ' · save ' + q.discount + '%' : '');
  }});
}}
if (periodSelect) periodSelect.addEventListener('change', showPrices);
document.querySelectorAll('[data-plan]').forEach((button) => button.addEventListener('click', async () => {{
  button.setAttribute('loading', '');
  try {{
    const token = await shopify.idToken();
    const res = await fetch('{APP_BASE}/plan-request', {{ method: 'POST', headers: {{ 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' }}, body: JSON.stringify({{ plan: button.dataset.plan, months: months() }}) }});
    if (!res.ok) throw new Error((await res.json().catch(() => ({{}}))).detail || res.statusText);
    const data = await res.json();
    if (data.checkout_url) {{
      // The payment page, at the top of the window (Stripe and Razorpay will not run in a frame).
      window.open(data.checkout_url, '_top');
      return;
    }}
    const pay = JSON.parse(button.dataset.pays || '{{}}')[String(months())] || button.dataset.pay;
    if (pay) window.open(pay, '_blank');
    shopify.toast.show('Plan request sent');
    setTimeout(() => location.reload(), 900);
  }} catch (error) {{
    button.removeAttribute('loading');
    shopify.toast.show('Could not send: ' + error.message, {{ isError: true }});
  }}
}}));
</script>"""


def _amount(payment: dict) -> str:
    amount = float(payment.get("amount") or 0)
    return f"{amount:,.2f} {payment.get('currency') or ''}".strip()


def _history_section(history: list[dict]) -> str:
    """Billing history: every payment, newest first."""
    if not history:
        return ""
    rows = "".join(
        "<s-table-row>"
        f"<s-table-cell>{html.escape(_nice_date(str(p.get('on') or '')))}</s-table-cell>"
        f"<s-table-cell>{html.escape(str((billing_plan_name(p.get('plan')))))}</s-table-cell>"
        f"<s-table-cell>{html.escape('1 month' if int(p.get('months') or 1) == 1 else str(p.get('months')) + ' months')}</s-table-cell>"
        f"<s-table-cell>{html.escape(_amount(p))}</s-table-cell>"
        f"<s-table-cell>{html.escape(_nice_date(str(p.get('paid_until') or '')))}</s-table-cell>"
        f"<s-table-cell>{html.escape(str(p.get('reference') or ''))}</s-table-cell>"
        "</s-table-row>"
        for p in history)
    return ('<s-section heading="Billing history"><s-table><s-table-header-row>'
            '<s-table-header listSlot="primary">Date</s-table-header><s-table-header>Plan</s-table-header>'
            '<s-table-header>Period</s-table-header><s-table-header format="currency">Amount</s-table-header>'
            '<s-table-header>Paid until</s-table-header><s-table-header>Reference</s-table-header>'
            f'</s-table-header-row><s-table-body>{rows}</s-table-body></s-table></s-section>')


def billing_plan_name(key) -> str:
    from app.services import billing
    plan = billing.plan_named(key)
    return plan["name"] if plan else str(key or "")


def _sign_in_page() -> HTMLResponse:
    """A page opened from the sidebar comes without the shop's session token:
    fetch one from App Bridge and load the page again with it."""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="shopify-api-key" content="{_attr(settings.SHOPIFY_CLIENT_ID)}">
<script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"></script>
<title>Pepa Assistant</title></head>
<body style="font: 15px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 24px; color: #616161">
<p id="m">Loading…</p>
<script>
(async () => {{
  try {{
    const token = await shopify.idToken();
    const url = new URL(location.href);
    url.searchParams.set('id_token', token);
    location.replace(url.toString());
  }} catch (e) {{
    document.getElementById('m').textContent = 'Open the app from your Shopify admin (Apps > Pepa AI Chatbot).';
  }}
}})();
</script></body></html>"""
    return HTMLResponse(page, headers={"Content-Security-Policy": "frame-ancestors https://admin.shopify.com https://*.myshopify.com;"})


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
    from app.services import billing
    shown = widget_settings.for_storefront(installs.settings_for(shops.current()))
    # A shop switched off for billing draws no launcher at all.
    shown["enabled"] = billing.plan_for(shops.current())["active"]
    return JSONResponse({"settings": shown},
                        headers={"Cache-Control": "public, max-age=60"})


@router.post("/shopify/app/plan-request", include_in_schema=False)
async def plan_request(request: Request) -> JSONResponse:
    """The merchant chose a plan on the Plan page."""
    try:
        shop = _shop_from_bearer(request)
    except installs.InstallError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    from app.services import billing
    try:
        months = int((body or {}).get("months") or 1)
    except (TypeError, ValueError):
        months = 1
    asked = await billing.request_plan(shop, str((body or {}).get("plan") or ""), months)
    if asked is None:
        return JSONResponse({"detail": "Unknown plan"}, status_code=400)
    logger.info("%s asked for the %s plan", shop, asked["plan"])
    from app.services import payments
    try:
        url = await payments.checkout(shop, asked["plan"], asked["months"])
    except payments.PaymentError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)
    return JSONResponse({"shop": shop, "requested": asked, "checkout_url": url})


@router.get("/shopify/app/{page}", response_class=HTMLResponse, include_in_schema=False)
async def shopify_app_page(page: str, request: Request) -> HTMLResponse:
    """Content, Appearance, Behaviour and Plan - the app's pages in the sidebar."""
    found = next(((key, label, groups) for key, label, groups in _APP_PAGES if key == page), None)
    if found is None:
        return _page("<h1>Pepa Assistant</h1><p>That page does not exist.</p>", status=404)
    id_token = request.query_params.get("id_token")
    if not id_token:
        return _sign_in_page()
    try:
        shop = await installs.install(id_token)
    except installs.InstallError as exc:
        return _page(f'<h1>Pepa Assistant</h1><p class="bad">{html.escape(str(exc))}</p>', status=400)
    key, label, groups = found
    if key == "plan":
        from app.services import billing
        info = await billing.for_merchant(shop)
        from app.services import payments
        body = _plan_sections(shop, info) + _history_section(payments.history_for(shop))
        if request.query_params.get("paid"):
            body = ('<s-banner heading="Thank you - payment received" tone="success"><s-paragraph>'
                    'Your plan is switched on as soon as the payment is confirmed, usually within a minute. '
                    'Refresh this page if it does not show yet.</s-paragraph></s-banner>') + body
        return _shell(shop, "Plan and billing", body, _PLAN_SCRIPT)
    values = widget_settings.for_shop(installs.settings_for(shop))
    return _form_page(shop, key, label, groups, values)
