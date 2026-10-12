"""Payment providers tell us a plan was paid for.

POST /payments/stripe     Stripe webhook (checkout.session.completed)
POST /payments/razorpay   Razorpay webhook (payment_link.paid)
GET/POST /payments/demo   our test checkout (PAYMENT_PROVIDER=demo only)

Each checks the provider's signature, then activates the plan for the months
paid (see services.payments). Anything unsigned or unrelated is ignored.
"""

import html
import logging

from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.services import payments

logger = logging.getLogger(__name__)
router = APIRouter(tags=["payments"])


async def _handle(name: str, request: Request, header: str) -> JSONResponse:
    body = await request.body()
    verify = payments.stripe_event if name == "stripe" else payments.razorpay_event
    event = verify(body, request.headers.get(header))
    if event is None:
        return JSONResponse({"detail": "Invalid signature"}, status_code=400)
    order = payments.paid_order(name, event)
    if order:
        await payments.activate(order)
    return JSONResponse({"ok": True})


@router.post("/payments/stripe", include_in_schema=False)
async def stripe_webhook(request: Request) -> JSONResponse:
    return await _handle("stripe", request, "stripe-signature")


@router.post("/payments/razorpay", include_in_schema=False)
async def razorpay_webhook(request: Request) -> JSONResponse:
    return await _handle("razorpay", request, "x-razorpay-signature")


_DEMO_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Test checkout</title>
<style>
:root {{ --bg:#f4f4f5; --card:#fff; --ink:#18181b; --muted:#71717a; --line:#e4e4e7; --accent:#18181b; --warn:#fef3c7; --warn-ink:#92400e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#09090b; --card:#18181b; --ink:#fafafa; --muted:#a1a1aa; --line:#27272a; --accent:#fafafa; --warn:#422006; --warn-ink:#fcd34d; }} }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, sans-serif; padding:16px; box-sizing:border-box; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:28px; width:100%; max-width:420px; box-sizing:border-box; }}
.tag {{ display:inline-block; background:var(--warn); color:var(--warn-ink); font-weight:600; font-size:12.5px; padding:3px 10px; border-radius:99px; }}
h1 {{ font-size:20px; margin:14px 0 4px; }} .muted {{ color:var(--muted); }}
.row {{ display:flex; justify-content:space-between; padding:10px 0; border-top:1px solid var(--line); }}
.total {{ font-weight:700; font-size:18px; }}
button {{ width:100%; padding:12px; border-radius:10px; font:inherit; font-weight:600; cursor:pointer; margin-top:10px; }}
.pay {{ background:var(--accent); color:var(--card); border:0; }} .cancel {{ background:transparent; color:var(--ink); border:1px solid var(--line); }}
</style></head><body><div class="card">
<span class="tag">TEST MODE - no money is taken</span>
<h1>Pepa Assistant</h1><div class="muted">{shop}</div>
<div style="margin-top:18px"><div class="row"><span>{label}</span><span></span></div>
<div class="row total"><span>Total</span><span>{amount}</span></div></div>
<form method="post"><input type="hidden" name="t" value="{token}">
<button class="pay" name="action" value="pay">Pay {amount} (test)</button>
<button class="cancel" name="action" value="cancel">Cancel</button></form>
</div></body></html>"""


@router.get("/payments/demo", response_class=HTMLResponse, include_in_schema=False)
async def demo_checkout(t: str = "") -> HTMLResponse:
    order = payments.demo_order(t) if payments.provider() == "demo" else None
    if not order:
        return HTMLResponse("<p>This test checkout has expired or is not available.</p>", status_code=404)
    amount = f"{order['amount'] / 100:,.2f} {order['currency']}"
    return HTMLResponse(_DEMO_PAGE.format(shop=html.escape(order["shop"]), label=html.escape(order["label"]),
                                          amount=html.escape(amount), token=html.escape(t)))


@router.post("/payments/demo", include_in_schema=False)
async def demo_checkout_answer(request: Request):
    form = parse_qs((await request.body()).decode())
    t, action = (form.get("t") or [""])[0], (form.get("action") or [""])[0]
    order = payments.demo_order(t) if payments.provider() == "demo" else None
    if not order:
        return HTMLResponse("<p>This test checkout has expired or is not available.</p>", status_code=404)
    if action != "pay":
        return RedirectResponse(payments.back_to_app(order["shop"]), status_code=303)
    back = await payments.demo_pay(order)
    return RedirectResponse(back + "?paid=1", status_code=303)
