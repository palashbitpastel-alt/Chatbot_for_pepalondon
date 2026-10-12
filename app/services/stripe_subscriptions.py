"""Plans that renew themselves: Stripe subscriptions.

- Choosing a plan opens Stripe Checkout in subscription mode. Stripe charges
  the card at the start of every period (1, 3, 6 or 12 months) and retries
  a failed renewal by itself.
- Changing plan or period updates the same subscription: Stripe credits the
  unused time and charges the difference straight away (proration). If the
  bank asks for confirmation, the merchant gets Stripe's invoice page.
- "Manage billing" opens Stripe's customer portal: card, cancel, invoices.

Stripe is the source of truth. Every webhook only tells us which
subscription changed; we then read that subscription from Stripe and copy
its state (plan, period end, status). So events arriving twice, late or out
of order can't leave a shop in the wrong state, and any server copy can
handle any event.

Prices: one Stripe Price per plan, period and amount, found by lookup key
(pepa_<plan>_<months>m_<currency>_<amount>) and created on first use, so a
price change in the plans simply makes a new Price and nothing is set up by
hand in Stripe.
"""

import logging
from datetime import datetime, timezone

import httpx

from app.core.config import settings
from app.services import billing, installs, shops

logger = logging.getLogger(__name__)

STATE_KEY = "_stripe"      # customer, subscription, status, ... on the shop's row
API = "https://api.stripe.com/v1/"
# Responses are read in this shape whatever the account's default version is.
VERSION = "2024-06-20"
LIVE = ("active", "trialing", "past_due")   # a subscription we change rather than start again

_price_ids: dict[str, str] = {}


class StripeError(RuntimeError):
    """Stripe said no. The message is safe to show."""


async def _call(method: str, path: str, data: dict | None = None, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(method, API + path, data=data, params=params,
                            headers={"Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}", "Stripe-Version": VERSION})
    if r.status_code >= 400:
        try:
            message = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = r.text[:200]
        logger.warning("Stripe %s %s answered %s: %s", method, path, r.status_code, message)
        raise StripeError(message)
    return r.json()


def state(shop: str) -> dict:
    return dict(installs.settings_for(shop).get(STATE_KEY) or {})


def _date(stamp) -> str:
    return datetime.fromtimestamp(int(stamp), tz=timezone.utc).date().isoformat() if stamp else ""


# ── Prices ──────────────────────────────────────────────────────────────────

async def price_id(plan: dict, months: int) -> str:
    currency = (settings.BILLING_CURRENCY or "USD").lower()
    amount = round(billing.quote(plan, months)["total"] * 100)
    key = f"pepa_{plan['key']}_{months}m_{currency}_{amount}"
    if key in _price_ids:
        return _price_ids[key]
    for attempt in range(2):
        found = await _call("GET", "prices", params={"lookup_keys[]": key, "active": "true", "limit": 1})
        if found.get("data"):
            _price_ids[key] = found["data"][0]["id"]
            return _price_ids[key]
        try:
            created = await _call("POST", "prices", data={
                "currency": currency, "unit_amount": str(amount),
                "recurring[interval]": "month", "recurring[interval_count]": str(months),
                "lookup_key": key, "product_data[name]": f"Pepa Assistant - {plan['name']} plan",
                "metadata[plan]": plan["key"], "metadata[months]": str(months)})
            _price_ids[key] = created["id"]
            return created["id"]
        except StripeError:
            if attempt:      # another server made it a moment ago: read it again
                raise
    raise StripeError("Could not set up the price")


# ── Starting, changing, managing ────────────────────────────────────────────

async def checkout(shop: str, plan: dict, months: int, back: str) -> str:
    """Stripe Checkout for a new subscription."""
    shop = shops.normalise(shop)
    meta = {"shop": shop, "plan": plan["key"], "months": str(months)}
    data = {
        "mode": "subscription",
        "line_items[0][price]": await price_id(plan, months),
        "line_items[0][quantity]": "1",
        "success_url": back + "?paid=1",
        "cancel_url": back,
        "client_reference_id": shop,
        # Name and address: Indian Stripe accounts need them on export payments.
        "billing_address_collection": "required",
        **{f"metadata[{k}]": v for k, v in meta.items()},
        **{f"subscription_data[metadata][{k}]": v for k, v in meta.items()},
    }
    customer = state(shop).get("customer")
    if customer:
        data["customer"] = customer
    return (await _call("POST", "checkout/sessions", data=data))["url"]


async def change(shop: str, plan: dict, months: int) -> dict:
    """Move a live subscription to another plan or period. Stripe credits the
    unused time and charges the difference now. Returns {"changed": True}, or
    {"checkout_url": ...} when the bank wants the merchant to confirm."""
    sub_id = state(shop).get("subscription")
    sub = await _call("GET", f"subscriptions/{sub_id}")
    item = sub["items"]["data"][0]
    new_price = await price_id(plan, months)
    if item["price"]["id"] == new_price:
        raise StripeError("That is already your plan.")
    updated = await _call("POST", f"subscriptions/{sub_id}", data={
        "items[0][id]": item["id"], "items[0][price]": new_price,
        "proration_behavior": "always_invoice",
        # The change only happens once the difference is paid.
        "payment_behavior": "pending_if_incomplete",
        "expand[]": "latest_invoice"})
    if updated.get("pending_update"):
        invoice = updated.get("latest_invoice") or {}
        if isinstance(invoice, dict) and invoice.get("hosted_invoice_url"):
            return {"checkout_url": invoice["hosted_invoice_url"]}
        raise StripeError("Your bank needs you to confirm this payment. Please try again.")
    if sub.get("cancel_at_period_end"):
        await _call("POST", f"subscriptions/{sub_id}", data={"cancel_at_period_end": "false"})
    await sync(sub_id)
    return {"changed": True}


async def portal(shop: str, back: str) -> str:
    customer = state(shop).get("customer")
    if not customer:
        raise StripeError("There is no billing account yet. Choose a plan first.")
    return (await _call("POST", "billing_portal/sessions", data={"customer": customer, "return_url": back}))["url"]


async def set_auto_renew(shop: str, on: bool) -> dict:
    """Off: the plan runs to the end of the paid period, then stops (Stripe's
    cancel at period end). On: it renews again."""
    sub_id = state(shop).get("subscription")
    if not sub_id or not is_live(shop):
        raise StripeError("There is no subscription to change.")
    await _call("POST", f"subscriptions/{sub_id}", data={"cancel_at_period_end": "false" if on else "true"})
    return await sync(sub_id) or {}


def is_live(shop: str) -> bool:
    return state(shop).get("status") in LIVE


# ── Copying Stripe's state ──────────────────────────────────────────────────

async def sync(sub_id: str) -> dict | None:
    """Read the subscription from Stripe and put its state on the shop."""
    sub = await _call("GET", f"subscriptions/{sub_id}")
    shop = (sub.get("metadata") or {}).get("shop")
    if not shop:
        return None
    shop = shops.normalise(shop)
    price = sub["items"]["data"][0]["price"]
    key = (price.get("metadata") or {}).get("plan") or (sub.get("metadata") or {}).get("plan")
    months = int((price.get("metadata") or {}).get("months") or price.get("recurring", {}).get("interval_count") or 1)
    period_end = _date(sub.get("current_period_end"))
    current = {"customer": sub.get("customer"), "subscription": sub["id"], "status": sub.get("status"),
               "plan": key, "months": months, "period_end": period_end,
               "cancel_at_period_end": bool(sub.get("cancel_at_period_end"))}
    previous = state(shop)
    if previous.get("subscription") and previous["subscription"] != sub["id"] and previous.get("status") in LIVE \
            and current["status"] not in LIVE:
        return None   # an old, ended subscription must not overwrite the live one
    await installs.save_private(shop, STATE_KEY, current)
    if current["status"] in ("active", "trialing") and billing.plan_named(key):
        # Paid up: the plan runs to the end of the period Stripe has charged for.
        saved = dict(installs.settings_for(shop).get(billing.KEY) or {})
        saved.update({"plan": key, "active": True, "chat_limit": None, "requested": None,
                      "paid_from": _date(sub.get("current_period_start")), "paid_until": period_end})
        await installs.save_private(shop, billing.KEY, saved)
    # past_due: Stripe is retrying; the paid-until date stays where it was and
    # the grace days then pause the shop if it never pays. canceled: the time
    # already paid for still runs out normally.
    logger.info("Stripe subscription %s for %s: %s, %s until %s", sub["id"], shop, current["status"], key, period_end)
    return {**current, "shop": shop}


def subscription_of(obj: dict) -> str | None:
    """The subscription id on an invoice or session, in old or new API shapes."""
    sub = obj.get("subscription")
    if not sub:
        sub = (((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription"))
    if isinstance(sub, dict):
        sub = sub.get("id")
    return sub or None


async def handle(event: dict) -> None:
    """A verified Stripe event about subscriptions."""
    kind, obj = event.get("type"), (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed" and obj.get("mode") == "subscription":
        sub_id = subscription_of(obj)
    elif kind in ("invoice.paid", "invoice.payment_failed"):
        sub_id = subscription_of(obj)
    elif kind in ("customer.subscription.updated", "customer.subscription.deleted", "customer.subscription.created"):
        sub_id = obj.get("id")
    else:
        return
    if not sub_id:
        return
    current = await sync(sub_id)
    if kind == "invoice.paid" and current and obj.get("amount_paid"):
        from app.services import payments
        await payments.record(current["shop"], {
            "plan": current["plan"], "months": current["months"],
            "amount": round(obj["amount_paid"] / 100, 2), "currency": str(obj.get("currency") or "").upper(),
            "reference": obj.get("id"), "provider": "stripe", "paid_until": current["period_end"],
            "invoice_url": obj.get("hosted_invoice_url") or ""})
