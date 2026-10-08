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
    body = (
        "<h1>Pepa Assistant</h1>"
        f"<p>Connected to <b>{html.escape(shop)}</b>. Switch the assistant on in your theme once, "
        "then set everything else here.</p>"
        f'<a class="btn" href="{html.escape(editor)}" target="_top">Switch it on in the theme editor</a>'
        '</div><div id="settings" class="card" style="margin-top:16px"><p class="muted">Loading settings…</p>'
    )
    body += _SETTINGS_APP.replace("__FIELDS__", json.dumps(widget_settings.FIELDS))
    return _page(body, shop)


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


# The settings form, drawn from widget_settings.FIELDS. Every call is signed
# with a fresh session token from App Bridge (shopify.idToken()), so only the
# shop's own admins can read or change its settings.
_SETTINGS_APP = """
<script>
const FIELDS = __FIELDS__;
const box = document.getElementById('settings');
const token = () => (window.shopify && shopify.idToken ? shopify.idToken() : Promise.reject(new Error('Open the app from your Shopify admin.')));
const call = async (method, body) => {
  const res = await fetch('app/settings', { method, headers: { 'Authorization': 'Bearer ' + await token(), 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
};
const LABELS = { right: 'Bottom right', left: 'Bottom left', label: 'Icon and text', icon: 'Icon only', sparkle: 'Sparkle', chat: 'Chat bubble', bag: 'Shopping bag', search: 'Search', default: "Pepa's own" };
function input(f, value) {
  const id = 'f_' + f.id;
  let el;
  if (f.type === 'textarea') { el = document.createElement('textarea'); el.rows = 4; el.value = value || ''; }
  else if (f.type === 'select') { el = document.createElement('select'); f.options.forEach(o => { const op = new Option(LABELS[o] || o, o); el.add(op); }); el.value = value; }
  else if (f.type === 'checkbox') { el = document.createElement('input'); el.type = 'checkbox'; el.checked = !!value; }
  else if (f.type === 'range') { el = document.createElement('input'); el.type = 'range'; el.min = f.min; el.max = f.max; el.value = value; }
  else if (f.type === 'color') { el = document.createElement('input'); el.type = 'color'; el.value = value || '#000000'; if (f.optional && !value) el.dataset.empty = '1'; el.addEventListener('input', () => { delete el.dataset.empty; sync(); }); }
  else { el = document.createElement('input'); el.type = f.type === 'url' ? 'url' : 'text'; el.value = value || ''; }
  el.id = id; el.dataset.field = f.id;
  const row = document.createElement('div'); row.className = 'row' + (f.type === 'checkbox' ? ' check' : '');
  const lab = document.createElement('label'); lab.htmlFor = id; lab.textContent = f.label;
  const out = document.createElement('span'); out.className = 'val';
  const sync = () => { if (f.type === 'range') out.textContent = el.value + (f.unit || ''); if (f.type === 'color') { out.textContent = el.dataset.empty ? 'follows main colours' : el.value; clear && (clear.hidden = !!el.dataset.empty); } };
  let clear = null;
  if (f.type === 'color' && f.optional) { clear = document.createElement('button'); clear.type = 'button'; clear.className = 'link'; clear.textContent = 'Clear'; clear.addEventListener('click', () => { el.dataset.empty = '1'; sync(); }); }
  el.addEventListener('input', sync);
  if (f.type === 'checkbox') { row.append(el, lab); } else { row.append(lab); const line = document.createElement('div'); line.className = 'line'; line.append(el); if (f.type === 'range' || f.type === 'color') line.append(out); if (clear) line.append(clear); row.append(line); }
  if (f.info) { const i = document.createElement('p'); i.className = 'muted'; i.textContent = f.info; row.append(i); }
  sync();
  return row;
}
function read() {
  const v = {};
  box.querySelectorAll('[data-field]').forEach(el => {
    const f = FIELDS.find(x => x.id === el.dataset.field);
    v[f.id] = f.type === 'checkbox' ? el.checked : f.type === 'range' ? Number(el.value) : (f.type === 'color' && el.dataset.empty) ? null : el.value;
  });
  return v;
}
(async () => {
  try {
    const { settings: values } = await call('GET');
    box.innerHTML = '';
    const groups = [...new Set(FIELDS.map(f => f.group))];
    groups.forEach((g, n) => {
      const d = document.createElement('details'); if (n < 2) d.open = true;
      const sum = document.createElement('summary'); sum.textContent = g; d.append(sum);
      FIELDS.filter(f => f.group === g).forEach(f => d.append(input(f, values[f.id])));
      box.append(d);
    });
    const bar = document.createElement('div'); bar.className = 'bar';
    const save = document.createElement('button'); save.className = 'btn'; save.textContent = 'Save';
    const note = document.createElement('span'); note.className = 'muted';
    save.addEventListener('click', async () => {
      save.disabled = true; note.textContent = 'Saving…';
      try { await call('POST', read()); note.textContent = 'Saved. Reload your store to see it.'; window.shopify && shopify.toast && shopify.toast.show('Settings saved'); }
      catch (e) { note.textContent = 'Could not save: ' + e.message; }
      save.disabled = false;
    });
    bar.append(save, note); box.append(bar);
  } catch (e) { box.innerHTML = '<p class="bad"></p>'; box.firstChild.textContent = 'Could not load the settings: ' + e.message; }
})();
</script>
<style>
  details { border-top: 1px solid #ebebeb; padding: 10px 0; }
  details:first-of-type { border-top: 0; }
  summary { cursor: pointer; font-weight: 600; padding: 4px 0; }
  .row { margin: 12px 0; }
  .row label { display: block; font-weight: 500; margin-bottom: 4px; }
  .row.check { display: flex; align-items: center; gap: 8px; }
  .row.check label { margin: 0; font-weight: 400; }
  .line { display: flex; align-items: center; gap: 10px; }
  input[type=text], input[type=url], textarea, select { width: 100%; box-sizing: border-box; padding: 7px 10px; border: 1px solid #c9c9c9; border-radius: 8px; font: inherit; }
  input[type=range] { flex: 1; }
  input[type=color] { width: 44px; height: 32px; padding: 0; border: 1px solid #c9c9c9; border-radius: 6px; background: none; }
  .val { font-size: 13px; color: #616161; min-width: 60px; }
  .link { border: 0; background: none; color: #005bd3; cursor: pointer; padding: 0; font: inherit; }
  .bar { position: sticky; bottom: 0; background: #fff; padding: 12px 0 4px; display: flex; align-items: center; gap: 12px; border-top: 1px solid #ebebeb; margin-top: 8px; }
  button.btn { border: 0; cursor: pointer; font: inherit; }
  button.btn[disabled] { opacity: .5; }
</style>
"""


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

