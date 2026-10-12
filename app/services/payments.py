"""Taking payment for a plan through Stripe or Razorpay, and activating it.

The app on custom distribution can't use Shopify's billing, so the merchant
pays us directly. When a merchant picks a plan and a period on the Plan page,
the backend opens a checkout for exactly that amount with the shop, plan and
period attached; when the payment provider tells us it was paid (a signed
webhook), the plan is switched on for those months with no one in between.

Which provider: whichever has its keys set (STRIPE_SECRET_KEY, or
RAZORPAY_KEY_ID + RAZORPAY_KEY_SECRET); PAYMENT_PROVIDER picks one when both
are. PAYMENT_PROVIDER=demo uses our own test checkout instead: no account, no
money, the same activation - for trying the flow before real keys exist. With neither, checkout() returns None and the Plan page falls back to a
request we accept by hand.

Plain HTTP to both providers' APIs - no SDK. Webhooks are verified against
the provider's signature before anything changes.
"""

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import date

import httpx

from app.core.config import settings
from app.services import billing, installs, shops

logger = logging.getLogger(__name__)

PAYMENTS_KEY = "_payments"   # billing history, newest first, on the shop's row


class PaymentError(RuntimeError):
    """The checkout could not be opened. Safe to show."""


def provider() -> str | None:
    chosen = (settings.PAYMENT_PROVIDER or "").strip().lower()
    if chosen == "demo":
        return "demo"
    stripe = bool(settings.STRIPE_SECRET_KEY)
    razorpay = bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)
    if chosen == "stripe" and stripe:
        return "stripe"
    if chosen == "razorpay" and razorpay:
        return "razorpay"
    return "stripe" if stripe else "razorpay" if razorpay else None


def _back_to_app(shop: str) -> str:
    store = shops.normalise(shop).split(".")[0]
    return f"https://admin.shopify.com/store/{store}/apps/{settings.SHOPIFY_APP_HANDLE}/api/v1/shopify/app/plan"


def _label(plan: dict, months: int) -> str:
    return f"{plan.get('name')} plan, {'1 month' if months == 1 else f'{months} months'}"


async def checkout(shop: str, plan_key: str, months: int) -> str | None:
    """A payment page for this shop, plan and period, or None when no provider is set."""
    which = provider()
    plan = billing.plan_named(plan_key)
    if not which or not plan or not plan.get("monthly"):
        return None
    months = months if months in billing.discounts() else 1
    amount = round(billing.quote(plan, months)["total"] * 100)   # smallest currency unit
    currency = (settings.BILLING_CURRENCY or "USD").upper()
    notes = {"shop": shops.normalise(shop), "plan": plan_key, "months": str(months)}
    back = _back_to_app(shop)
    if which == "demo":
        token = demo_token({**notes, "amount": amount, "currency": currency,
                            "label": _label(plan, months), "ref": "test_" + secrets.token_hex(6)})
        # Relative: the Plan page (served by this backend) opens it on our own host.
        return f"/api/v1/payments/demo?t={token}"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            if which == "stripe":
                form = {
                    "mode": "payment",
                    "success_url": back + "?paid=1",
                    "cancel_url": back,
                    "client_reference_id": notes["shop"],
                    "line_items[0][quantity]": "1",
                    "line_items[0][price_data][currency]": currency.lower(),
                    "line_items[0][price_data][unit_amount]": str(amount),
                    "line_items[0][price_data][product_data][name]": "Pepa Assistant - " + _label(plan, months),
                    **{f"metadata[{k}]": v for k, v in notes.items()},
                }
                response = await c.post("https://api.stripe.com/v1/checkout/sessions", data=form,
                                        headers={"Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}"})
                response.raise_for_status()
                return response.json()["url"]
            response = await c.post(
                "https://api.razorpay.com/v1/payment_links",
                auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
                json={"amount": amount, "currency": currency, "accept_partial": False,
                      "description": "Pepa Assistant - " + _label(plan, months),
                      "notes": notes, "callback_url": back + "?paid=1", "callback_method": "get"})
            response.raise_for_status()
            return response.json()["short_url"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Could not open a %s checkout for %s: %s", which, shop, exc)
        raise PaymentError("The payment page could not be opened just now. Please try again.") from exc


# ── Test checkout (PAYMENT_PROVIDER=demo) ───────────────────────────────────
# The order travels in a signed token, so the test page can't be used to
# pay for something other than what the merchant picked.

def demo_token(order: dict) -> str:
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({**order, "exp": int(time.time()) + 3600}).encode()).decode().rstrip("=")
    sig = hmac.new(settings.SHOPIFY_CLIENT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def demo_order(token: str) -> dict | None:
    import base64
    payload, _, sig = (token or "").partition(".")
    expected = hmac.new(settings.SHOPIFY_CLIENT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not payload or not hmac.compare_digest(expected, sig):
        return None
    try:
        order = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    return order if order.get("exp", 0) > time.time() else None


async def demo_pay(order: dict) -> str:
    """Pay the test order: the same activation a real payment gets. Returns where to go next."""
    await activate({"shop": shops.normalise(order["shop"]), "plan": order["plan"], "months": int(order["months"]),
                    "amount": round(order["amount"] / 100, 2), "currency": order["currency"],
                    "reference": order["ref"], "provider": "test"})
    return _back_to_app(order["shop"])


def back_to_app(shop: str) -> str:
    return _back_to_app(shop)


# ── Webhooks ────────────────────────────────────────────────────────────────

def stripe_event(body: bytes, header: str | None) -> dict | None:
    """The verified Stripe event, or None. Stripe signs "<t>.<body>" with the
    endpoint's secret; old signatures (over 5 minutes) are refused."""
    secret = settings.STRIPE_WEBHOOK_SECRET
    if not (secret and header):
        return None
    pairs = [p.strip().split("=", 1) for p in header.split(",") if "=" in p]
    stamp = next((v for k, v in pairs if k == "t"), None)
    signatures = [v for k, v in pairs if k == "v1"]
    if not stamp or not stamp.isdigit() or not signatures or abs(time.time() - int(stamp)) > 300:
        return None
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        return None
    return json.loads(body)


def razorpay_event(body: bytes, header: str | None) -> dict | None:
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not (secret and header):
        return None
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return json.loads(body) if hmac.compare_digest(expected, header) else None


def paid_order(provider_name: str, event: dict) -> dict | None:
    """(shop, plan, months, amount, currency, reference) from a paid event."""
    try:
        if provider_name == "stripe":
            if event.get("type") != "checkout.session.completed":
                return None
            obj = event["data"]["object"]
            if obj.get("payment_status") != "paid":
                return None
            meta, amount, currency, ref = obj.get("metadata") or {}, obj.get("amount_total"), obj.get("currency"), obj.get("id")
        else:
            if event.get("event") != "payment_link.paid":
                return None
            link = event["payload"]["payment_link"]["entity"]
            meta, amount, currency, ref = link.get("notes") or {}, link.get("amount_paid") or link.get("amount"), link.get("currency"), link.get("id")
    except (KeyError, TypeError):
        return None
    if not (meta.get("shop") and meta.get("plan")):
        return None
    return {"shop": shops.normalise(meta["shop"]), "plan": meta["plan"], "months": int(meta.get("months") or 1),
            "amount": round((amount or 0) / 100, 2), "currency": str(currency or "").upper(), "reference": ref,
            "provider": provider_name}


async def activate(order: dict) -> bool:
    """Switch the paid plan on and keep the payment in the shop's history.
    A payment already recorded (the provider retrying) changes nothing."""
    shop = order["shop"]
    history = list(installs.settings_for(shop).get(PAYMENTS_KEY) or [])
    if any(p.get("reference") == order["reference"] for p in history):
        return False
    saved = dict(installs.settings_for(shop).get(billing.KEY) or {})
    saved["requested"] = {"plan": order["plan"], "months": order["months"], "on": date.today().isoformat()}
    await installs.save_private(shop, billing.KEY, saved)
    plan = await billing.answer_request(shop, True, order["months"])
    history.insert(0, {"on": date.today().isoformat(), "plan": order["plan"], "months": order["months"],
                       "amount": order["amount"], "currency": order["currency"], "reference": order["reference"],
                       "provider": order["provider"], "paid_until": plan.get("paid_until")})
    await installs.save_private(shop, PAYMENTS_KEY, history[:50])
    logger.info("Payment %s activated %s on %s until %s", order["reference"], order["plan"], shop, plan.get("paid_until"))
    return True


def history_for(shop: str) -> list[dict]:
    return list(installs.settings_for(shop).get(PAYMENTS_KEY) or [])
