"""Payment providers tell us a plan was paid for.

POST /payments/stripe     Stripe webhook (checkout.session.completed)
POST /payments/razorpay   Razorpay webhook (payment_link.paid)

Each checks the provider's signature, then activates the plan for the months
paid (see services.payments). Anything unsigned or unrelated is ignored.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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
