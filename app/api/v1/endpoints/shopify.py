import html
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.config import settings
from app.core.security import require_admin
from app.services import installs, insights, shopify_search, shops, widget_settings
from app.services.shopify_store import fetch_store_snapshot, is_configured

logger = logging.getLogger(__name__)

router = APIRouter(tags=["shopify"])


@router.get("/shopify/status", dependencies=[Depends(require_admin)])
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
        from app.services import retention
        logger.info("Webhook shop/redact from %s: deleted %s", shop, await retention.forget_shop(shops.normalise(shop)))
    elif topic in ("customers/redact", "customers/data_request"):
        # Chats are kept under random ids, but shoppers type their email into
        # them: a conversation that mentions this customer's email is theirs.
        try:
            customer = (json.loads(body or b"{}").get("customer") or {})
        except ValueError:
            customer = {}
        if topic == "customers/redact":
            from app.services import retention
            gone = await retention.forget_customer(shops.normalise(shop), customer.get("email"),
                                                   str(customer.get("id") or "") or None)
            logger.info("Webhook customers/redact from %s: deleted %s", shop, gone)
        else:
            # Shopify gives 30 days to send the customer their data: flagged
            # here for a person to act on (the id only - no email in the logs).
            logger.warning("ACTION NEEDED: customers/data_request from %s for customer id %s", shop, customer.get("id"))
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


_LIVE = ("active", "trialing", "past_due")


def _renewing(info: dict) -> bool:
    sub = info.get("subscription") or {}
    return sub.get("status") in ("active", "trialing") and not sub.get("cancel_at_period_end")


def _change_note(change: dict) -> dict:
    """The quote plus one line on what paying does to the dates."""
    kind, ends = change.get("kind"), _nice_date(change.get("ends") or "")
    if kind == "renew":
        note = f"Adds to your current plan: runs until {ends}."
    elif kind == "change":
        note = (f"Starts today. Your {change['days_left']} days of {change['from_plan']} left "
                f"become {change['extra_days']} extra days: runs until {ends}.")
    else:
        note = f"Starts today: runs until {ends}." if ends else ""
    return {**change, "note": note}


def _plan_sections(shop: str, info: dict) -> str:
    """The subscription part of the app's page: current plan, usage, paid-until,
    how to pay, and the plans to move to."""
    plan = info.get("plan")
    used, limit, pct = info["chats_used"], info.get("chat_limit"), info.get("percent")
    pay = info.get("payment_url") or ""
    contact = settings.SUPPORT_CONTACT_EMAIL
    days = info.get("days_left")
    banners = []
    ended = _nice_date(info.get("paid_until") or "")
    if not info["active"]:
        banners.append(("critical", "The assistant is paused on your store",
                        "Shoppers can't chat until your plan is renewed. Renew below or contact us."))
    elif info.get("lapsed"):
        banners.append(("critical", f"The assistant is paused: your plan ended on {ended}",
                        "Shoppers can't chat right now. Renew below and it switches back on straight away."))
    elif days is not None and days < 0:
        banners.append(("critical", f"Your plan ended on {ended}",
                        f"The assistant keeps running until {_nice_date(info.get('grace_until') or '')}, then pauses. "
                        "Renew now to avoid a break."))
    elif days is not None and days <= 7 and not _renewing(info):
        banners.append(("warning", "Your plan ends today" if days == 0 else f"Your plan ends in {days} day{'s' if days != 1 else ''}",
                        "Renew below before then to keep the assistant running without a break."))
    sub = info.get("subscription") or {}
    if sub.get("status") == "past_due":
        banners.insert(0, ("critical", "Your last payment didn't go through",
                           "Stripe will try again over the next few days. Update your card in Manage billing "
                           "to keep the assistant running."))
    elif sub.get("status") in _LIVE and sub.get("cancel_at_period_end"):
        banners.append(("info", "Auto-renew is off",
                        f"Your plan ends on {_nice_date(sub.get('period_end') or '')}. Turn auto-renew back on to keep it."))
    if info.get("running") and pct is not None and pct >= 100:
        banners.append(("critical", "You have used all of this month's chats",
                        "New conversations are paused until next month. Upgrade to continue now."))
    elif info.get("running") and pct is not None and pct >= 80:
        banners.append(("warning", f"You have used {pct}% of this month's chats",
                        "Upgrade before you reach the limit so shoppers can keep chatting."))
    banner_html = "".join(
        f'<s-banner heading="{_attr(h)}" tone="{tone}"><s-paragraph>{html.escape(t)}</s-paragraph></s-banner>'
        for tone, h, t in banners)
    status = ('<s-badge tone="success">Active</s-badge>' if info.get("running", info["active"])
              else '<s-badge tone="critical">Paused</s-badge>')
    plan_box = (f'<s-text color="subdued">Current plan</s-text>'
                f'<s-stack direction="inline" gap="small-200" alignItems="center"><s-heading>{html.escape(plan["name"] if plan else "No plan yet")}</s-heading>{status}</s-stack>'
                f'<s-text>{html.escape(plan["price"]) if plan else "Choose a plan below"}</s-text>')
    usage = f"{used:,} of {limit:,}" if limit else f"{used:,}"
    bar = (f'<div class="pa-meter" role="img" aria-label="{pct}% used"><div style="width:{pct}%"'
           f'{" class=hot" if pct >= 80 else ""}></div></div>') if pct is not None else '<s-text color="subdued">No monthly limit</s-text>'
    renew_line = ""
    if _renewing(info):
        renew_line = f'<s-badge tone="success">Auto-renews on {html.escape(_nice_date(sub.get("period_end") or ""))}</s-badge>'
    elif sub.get("status") in _LIVE:
        renew_line = '<s-badge>Auto-renew off</s-badge>'
    paid = (f'<s-heading>{html.escape(_time_left(days))}</s-heading>'
            f'{_time_bar(info)}'
            f'<s-text color="subdued">{html.escape(("Ended " if days is not None and days < 0 else "Until ") + _nice_date(info["paid_until"]))}</s-text>'
            f'{renew_line}'
            ) if info.get("paid_until") else '<s-heading>–</s-heading><s-text color="subdued">Not set yet</s-text>'
    box = lambda inner: f'<s-box padding="base" border="base" borderRadius="base"><s-stack gap="small-200">{inner}</s-stack></s-box>'
    actions = []
    if sub.get("customer"):
        actions.append('<s-button data-portal>Manage billing</s-button>')
    if sub.get("status") in _LIVE:
        on = not sub.get("cancel_at_period_end")
        actions.append(f'<s-button data-renew="{"0" if on else "1"}"{"" if on else " variant=\"primary\""}>'
                       f'{"Turn off auto-renew" if on else "Turn on auto-renew"}</s-button>')
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
        label = "Renew" if current else "Upgrade" if better and plan else "Choose" if not plan else "Switch"
        live_sub = sub.get("status") in _LIVE
        if live_sub:
            label = "Switch period" if p.get("key") == sub.get("plan") else ("Upgrade" if better else "Switch")
        if p.get("key") == requested:
            button = '<s-badge tone="warning">Requested</s-badge>'
        else:
            button = ("" if current and not p.get("monthly") else
                      f'<s-button data-plan="{_attr(p.get("key"))}" data-pay="{_attr(p.get("payment_url") or pay or "")}"'
                      f'{" variant=\"primary\"" if better else ""}>{label}</s-button>')
        features = "".join(f"<s-list-item>{html.escape(str(f))}</s-list-item>" for f in p.get("features") or [])
        from app.services import billing as _billing
        quotes = ({str(q["months"]): _change_note(_billing.change_quote(shop, p["key"], q["months"]))
                   for q in info.get("periods") or []} if p.get("monthly") else {})
        pays = {str(k): v for k, v in (p.get("payment_urls") or {}).items()} if isinstance(p.get("payment_urls"), dict) else {}
        button = button.replace("<s-button data-plan=", f"<s-button data-pays=\"{_attr(json.dumps(pays))}\" data-plan=", 1)
        if live_sub:
            button = button.replace("<s-button data-pays=", '<s-button data-confirm="' + _attr(
                "Switch now? Stripe credits the unused part of your current plan and charges the difference "
                "to your card today.") + '" data-pays=', 1)
        if info.get("billing_mode") == "stripe":
            for m, q in quotes.items():
                every = "month" if m == "1" else f"{m} months"
                q["note"] = (f"Switching: Stripe credits your unused time and charges the difference today. "
                             f"Then renews every {every}." if live_sub else
                             f"Renews automatically every {every}. Turn off any time.")
        cards.append(box(
            f'<s-stack direction="inline" gap="small-200" alignItems="center"><s-heading>{html.escape(str(p.get("name")))}</s-heading>'
            f'{"<s-badge tone=\"info\">Current plan</s-badge>" if current else ""}</s-stack>'
            f'<s-text type="strong" data-price="{_attr(p.get("key"))}" data-quotes="{_attr(json.dumps(quotes))}">{html.escape(str(p.get("price") or ""))}</s-text>'
            f'<s-text color="subdued" data-total="{_attr(p.get("key"))}">Billed monthly</s-text>'
            f'<s-unordered-list>{features}</s-unordered-list>'
            f'<s-text color="subdued" data-note="{_attr(p.get("key"))}">{html.escape((quotes.get("1") or {}).get("note", ""))}</s-text>{button}'))
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
    status = ('<s-badge tone="success">Active</s-badge>' if info.get("running", info["active"])
              else '<s-badge tone="warning">Paused by you</s-badge>' if info.get("merchant_paused")
              else '<s-badge tone="critical">Paused</s-badge>')
    paused_by_you = info.get("merchant_paused")
    switch = (f'<s-button data-assistant="{"1" if paused_by_you else "0"}"{" variant=\"primary\"" if paused_by_you else ""}>'
              f'{"Turn on assistant" if paused_by_you else "Pause assistant"}</s-button>')
    box = lambda inner: f'<s-box padding="base" border="base" borderRadius="base"><s-stack gap="small-200">{inner}</s-stack></s-box>'
    cards = (
        '<s-grid gridTemplateColumns="repeat(auto-fit, minmax(200px, 1fr))" gap="base">'
        + box(f'<s-text color="subdued">Assistant</s-text><s-stack direction="inline" gap="small-200" alignItems="center"><s-heading>{html.escape(values.get("assistant_name") or "Assistant")}</s-heading>{status}</s-stack>'
              f'<s-stack direction="inline">{switch}</s-stack>')
        + box(f'<s-text color="subdued">Chats this month</s-text><s-heading>{used:,}{f" of {limit:,}" if limit else ""}</s-heading>')
        + box(f'<s-text color="subdued">Plan</s-text><s-heading>{html.escape(plan["name"] if plan else "No plan yet")}</s-heading>'
              + (f'<s-text>{html.escape(_time_left(info.get("days_left")))}</s-text>{_time_bar(info)}' if info.get("paid_until") else "")
              + f'<s-link href="{APP_BASE}/plan">See plan and usage</s-link>')
        + '</s-grid>')
    links = "".join(
        box(f'<s-heading>{html.escape(label)}</s-heading><s-paragraph color="subdued">{html.escape(_PAGE_HELP[key])}</s-paragraph>'
            f'<s-link href="{APP_BASE}/{key}">Open {html.escape(label.lower())}</s-link>')
        for key, label, _ in _APP_PAGES)
    alert = ""
    days = info.get("days_left")
    if paused_by_you:
        alert = ('<s-banner heading="The assistant is paused" tone="warning"><s-paragraph>'
                 'You paused it: shoppers don\'t see the chat on your store. Your plan is not affected. '
                 'Turn it on again whenever you like.</s-paragraph></s-banner>')
    if info.get("lapsed") or (info["active"] and days is not None and days < 0):
        heading = ("The assistant is paused: your plan has ended" if info.get("lapsed")
                   else f"Your plan has ended - the assistant pauses after {_nice_date(info.get('grace_until') or '')}")
        alert = (f'<s-banner heading="{_attr(heading)}" tone="critical"><s-stack direction="inline">'
                 f'<s-button href="{APP_BASE}/plan" variant="primary">Renew</s-button></s-stack></s-banner>')
    body = (
        alert
        + cards
        + f'<s-banner heading="Switch the assistant on in your theme" tone="info"><s-stack gap="small-200">'
          f'<s-paragraph>Connected to {html.escape(shop)}. Turn on Pepa Assistant under App embeds once; set everything else here in the app.</s-paragraph>'
          f'<s-stack direction="inline"><s-button href="{_attr(editor)}" target="_top">Open the theme editor</s-button></s-stack></s-stack></s-banner>'
        + f'<s-section heading="Set up your assistant"><s-grid gridTemplateColumns="repeat(auto-fit, minmax(220px, 1fr))" gap="base">{links}</s-grid></s-section>')
    return _shell(shop, values.get("assistant_name") or "Pepa Assistant", body, _ASSISTANT_SCRIPT)


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
    const note = document.querySelector('[data-note="' + el.dataset.price + '"]');
    if (note) note.textContent = q.note || '';
  }});
}}
if (periodSelect) periodSelect.addEventListener('change', showPrices);
async function post(path, body) {{
  const token = await shopify.idToken();
  const res = await fetch('{APP_BASE}/' + path, {{ method: 'POST', headers: {{ 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' }}, body: JSON.stringify(body || {{}}) }});
  const data = await res.json().catch(() => ({{}}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}}
document.querySelectorAll('[data-portal]').forEach((button) => button.addEventListener('click', async () => {{
  button.setAttribute('loading', '');
  try {{ window.open((await post('billing-portal')).url, '_top'); }}
  catch (error) {{ button.removeAttribute('loading'); shopify.toast.show(error.message, {{ isError: true }}); }}
}}));
document.querySelectorAll('[data-renew]').forEach((button) => button.addEventListener('click', async () => {{
  const on = button.dataset.renew === '1';
  if (!on && !confirm('Turn off auto-renew? Your plan keeps running until the end of the period you have paid for, then stops.')) return;
  button.setAttribute('loading', '');
  try {{ await post('auto-renew', {{ on }}); shopify.toast.show(on ? 'Auto-renew is on' : 'Auto-renew is off'); setTimeout(() => location.reload(), 900); }}
  catch (error) {{ button.removeAttribute('loading'); shopify.toast.show(error.message, {{ isError: true }}); }}
}}));
document.querySelectorAll('[data-plan]').forEach((button) => button.addEventListener('click', async () => {{
  if (button.dataset.confirm && !confirm(button.dataset.confirm)) return;
  button.setAttribute('loading', '');
  try {{
    const token = await shopify.idToken();
    const res = await fetch('{APP_BASE}/plan-request', {{ method: 'POST', headers: {{ 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' }}, body: JSON.stringify({{ plan: button.dataset.plan, months: months() }}) }});
    if (!res.ok) throw new Error((await res.json().catch(() => ({{}}))).detail || res.statusText);
    const data = await res.json();
    if (data.changed) {{
      shopify.toast.show('Plan switched');
      setTimeout(() => location.reload(), 900);
      return;
    }}
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


# Pause / turn on the assistant from the Home page.
_ASSISTANT_SCRIPT = f"""<script>
document.querySelectorAll('[data-assistant]').forEach((button) => button.addEventListener('click', async () => {{
  const on = button.dataset.assistant === '1';
  if (!on && !confirm('Pause the assistant? The chat disappears from your store until you turn it on again. Your plan is not affected.')) return;
  button.setAttribute('loading', '');
  try {{
    const res = await fetch('{APP_BASE}/assistant', {{ method: 'POST', headers: {{ 'Authorization': 'Bearer ' + (await shopify.idToken()), 'Content-Type': 'application/json' }}, body: JSON.stringify({{ on }}) }});
    if (!res.ok) throw new Error((await res.json().catch(() => ({{}}))).detail || res.statusText);
    shopify.toast.show(on ? 'Assistant turned on' : 'Assistant paused');
    setTimeout(() => location.reload(), 900);
  }} catch (error) {{
    button.removeAttribute('loading');
    shopify.toast.show('Could not change it: ' + error.message, {{ isError: true }});
  }}
}}));
</script>"""


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
    shown["enabled"] = billing.plan_for(shops.current())["running"]
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
    key = str((body or {}).get("plan") or "")
    if not billing.plan_named(key):
        return JSONResponse({"detail": "Unknown plan"}, status_code=400)
    from app.services import payments
    if payments.provider() == "stripe":
        from app.services import stripe_subscriptions as subs
        if subs.is_live(shop):
            # Already subscribed: switch the same subscription (Stripe prorates).
            try:
                result = await subs.change(shop, billing.plan_named(key), months if months in billing.discounts() else 1)
            except subs.StripeError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            logger.info("%s switched to the %s plan, %s months", shop, key, months)
            return JSONResponse({"shop": shop, **result})
    if payments.provider():
        # Paying online: nothing is recorded until the payment arrives, so a
        # closed checkout leaves the page as it was.
        try:
            url = await payments.checkout(shop, key, months)
        except payments.PaymentError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)
        if url:
            logger.info("%s opened a checkout for the %s plan, %s months", shop, key, months)
            return JSONResponse({"shop": shop, "checkout_url": url})
    asked = await billing.request_plan(shop, key, months)
    logger.info("%s asked for the %s plan", shop, asked["plan"])
    return JSONResponse({"shop": shop, "requested": asked})


async def _stripe_action(request: Request, act) -> JSONResponse:
    try:
        shop = _shop_from_bearer(request)
    except installs.InstallError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    from app.services import stripe_subscriptions as subs, payments
    if payments.provider() != "stripe":
        return JSONResponse({"detail": "Online billing is not switched on."}, status_code=400)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    try:
        return JSONResponse({"shop": shop, **(await act(subs, shop, body or {}, payments.back_to_app(shop)))})
    except subs.StripeError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@router.post("/shopify/app/assistant", include_in_schema=False)
async def assistant_switch(request: Request) -> JSONResponse:
    """The merchant pauses or turns on the assistant on their store."""
    try:
        shop = _shop_from_bearer(request)
    except installs.InstallError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    from app.services import billing
    on = bool((body or {}).get("on"))
    await installs.save_private(shop, billing.MERCHANT_PAUSE_KEY, not on)
    logger.info("%s %s the assistant", shop, "turned on" if on else "paused")
    return JSONResponse({"shop": shop, "on": on, "running": billing.plan_for(shop)["running"]})


@router.post("/shopify/app/billing-portal", include_in_schema=False)
async def billing_portal(request: Request) -> JSONResponse:
    """Stripe's customer portal: card, invoices, cancel."""
    async def act(subs, shop, body, back):
        return {"url": await subs.portal(shop, back)}
    return await _stripe_action(request, act)


@router.post("/shopify/app/auto-renew", include_in_schema=False)
async def auto_renew(request: Request) -> JSONResponse:
    """Turn the plan's automatic renewal off or back on."""
    async def act(subs, shop, body, back):
        current = await subs.set_auto_renew(shop, bool(body.get("on")))
        return {"auto_renew": not current.get("cancel_at_period_end")}
    return await _stripe_action(request, act)


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
        if payments.provider():
            info["requested"] = None   # online payment: a request waits on no one
        if payments.provider() == "stripe":
            from app.services import stripe_subscriptions as subs
            info["billing_mode"] = "stripe"
            info["subscription"] = subs.state(shop) if subs.state(shop).get("subscription") else None
        body = _plan_sections(shop, info) + _history_section(payments.history_for(shop))
        if request.query_params.get("paid"):
            body = ('<s-banner heading="Thank you - payment received" tone="success"><s-paragraph>'
                    'Your plan is switched on as soon as the payment is confirmed, usually within a minute. '
                    'Refresh this page if it does not show yet.</s-paragraph></s-banner>') + body
        return _shell(shop, "Plan and billing", body, _PLAN_SCRIPT)
    values = widget_settings.for_shop(installs.settings_for(shop))
    return _form_page(shop, key, label, groups, values)
