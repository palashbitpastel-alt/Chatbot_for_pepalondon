import html
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.config import settings
from app.services import installs, insights, shopify_search
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
        "<h1>Pepa Assistant is installed</h1>"
        f"<p>Your store <b>{html.escape(shop)}</b> is connected. The assistant reads your products, "
        "collections and policies straight from Shopify.</p>"
        "<p>One step left: switch it on in your theme.</p>"
        "<ol><li>Open the theme editor with the button below.</li>"
        "<li>Turn on <b>Pepa Assistant</b> under App embeds.</li>"
        "<li>Click <b>Save</b>. Colours, fonts and texts are set in the same panel.</li></ol>"
        f'<a class="btn" href="{html.escape(editor)}" target="_top">Switch it on in the theme editor</a>'
        '<p class="muted" style="margin-top:16px">Removing the app disconnects your store and deletes its access.</p>'
    )
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

