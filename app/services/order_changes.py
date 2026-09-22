"""Letting a shopper cancel an order or move its delivery address, from the chat.

Everything else the support agent touches is a read. These are writes: one
refunds money and restocks goods, the other redirects a parcel that has already
been paid for. So they are deliberately harder to reach than a status lookup.

The gate is two-step and server-side:

1. ``begin`` finds the order the same non-probing way ``find_order`` does - the
   order number and the email must match, and a wrong email is indistinguishable
   from an order that does not exist. It checks the change is even possible, then
   records the pending change against this conversation and returns a *challenge*:
   one detail printed on the order that the buyer has and a guesser does not. The
   answer is never returned, and only its hash is stored.

2. ``commit`` takes the answer, and finds the pending change itself. Wrong answers
   burn attempts, it is dropped after three, and it expires on its own after a few
   minutes. Only then does a mutation run.

Why a challenge at all: the storefront widget sends ``verified: false`` on
purpose, because a browser posting an email proves nothing. Order numbers are
close to sequential and email addresses leak, so those two together are a weak
secret - fine for showing someone where their parcel is, not for moving it. The
postcode already on the order is knowledge the buyer holds; asking for it turns
"knows two guessable things" into "is holding the confirmation email".

The pending change is keyed by the chat session and kept in the database, not
handed to the agent as a token. The agent could not hold one anyway: only the
user and assistant turns are replayed into its context, so a tool result from
three turns ago is gone by the time the shopper says "yes" - which is exactly
how the first version of this failed, reporting "expired" on a two-minute
conversation. Keeping it server-side also means the secret never reaches the
model, and the row survives the redeploy that an in-process dict would not.
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.core.config import settings
from app.db.models import OrderChangeRequest
from app.db.session import AsyncSessionLocal
from app.services import shopify_client
from app.services.shopify_client import ShopifyError, graphql
from app.services.shopify_storefront import order_number_variants

logger = logging.getLogger(__name__)

CANCEL = "cancel"
CHANGE_ADDRESS = "change_address"
ACTIONS = (CANCEL, CHANGE_ADDRESS)

# What we offer rather than making someone invent a phrase. `other` is what
# makes the free-text box legitimate instead of a fallback nobody reaches.
CANCEL_REASONS = [
    {"code": "changed_mind", "label": "I changed my mind"},
    {"code": "wrong_item", "label": "I ordered the wrong item or size"},
    {"code": "ordered_twice", "label": "I ordered twice by mistake"},
    {"code": "too_slow", "label": "It is taking too long to arrive"},
    {"code": "found_cheaper", "label": "I found it cheaper elsewhere"},
    {"code": "no_longer_needed", "label": "I no longer need it"},
    {"code": "other", "label": "Something else"},
]

ADDRESS_REASONS = [
    {"code": "typo", "label": "I mistyped the address"},
    {"code": "moved", "label": "I have moved since ordering"},
    {"code": "send_elsewhere", "label": "Send it to a different address"},
    {"code": "work_address", "label": "Send it to my work address"},
    {"code": "gift", "label": "It is a gift and should go to them"},
    {"code": "other", "label": "Something else"},
]

_REASON_CODES = {r["code"] for r in CANCEL_REASONS} | {r["code"] for r in ADDRESS_REASONS}

NO_PENDING = (
    "I have lost track of that request. Give me the order number and email again and we "
    "can pick it straight back up."
)

# Shopify's own enum is coarse. Every one of ours is the customer asking, so the
# shopper's actual words go in the staff note where a human will read them.
SHOPIFY_CANCEL_REASON = "CUSTOMER"

# Cancelling refunds to the original payment method and restocks. Both are what
# a shopper means by "cancel", and leaving either off quietly turns a
# cancellation into a support ticket. Store credit is the other option Shopify
# offers, and is not what someone asking to cancel is expecting.
REFUND_ON_CANCEL = True
RESTOCK_ON_CANCEL = True
NOTIFY_ON_CANCEL = True

REASON_NOTE_LIMIT = 400

# Fulfilment states where nothing has physically left yet.
UNSTARTED = {"UNFULFILLED", "ON_HOLD", "SCHEDULED", "OPEN"}


ORDER_FOR_CHANGE = """
query SupportOrderForChange($query: String!) {
  orders(first: 1, query: $query) {
    nodes {
      id
      name
      email
      note
      tags
      createdAt
      cancelledAt
      displayFulfillmentStatus
      displayFinancialStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      shippingAddress {
        firstName lastName address1 address2 city zip
        provinceCode countryCodeV2 phone
      }
      lineItems(first: 10) { nodes { title quantity } }
    }
  }
}
"""

# `refundMethod` replaced the old boolean `refund` argument - passing the old
# shape is rejected outright, so this operation is tied to a current API version.
ORDER_CANCEL = """
mutation SupportOrderCancel(
  $orderId: ID!, $reason: OrderCancelReason!, $refundMethod: OrderCancelRefundMethodInput!,
  $restock: Boolean!, $staffNote: String, $notifyCustomer: Boolean
) {
  orderCancel(
    orderId: $orderId, reason: $reason, refundMethod: $refundMethod,
    restock: $restock, staffNote: $staffNote, notifyCustomer: $notifyCustomer
  ) {
    job { id done }
    orderCancelUserErrors { field message code }
  }
}
"""

ORDER_UPDATE_ADDRESS = """
mutation SupportOrderAddress($input: OrderInput!) {
  orderUpdate(input: $input) {
    order {
      id
      name
      shippingAddress { address1 address2 city zip provinceCode countryCodeV2 }
    }
    userErrors { field message }
  }
}
"""


# ── The pending change ─────────────────────────────────────────────────────


def _hash(answer: str) -> str:
    return hashlib.sha256(answer.encode("utf-8")).hexdigest()


async def _save(session_id: str, **fields) -> None:
    """One pending change per session; starting another replaces it."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(OrderChangeRequest).where(OrderChangeRequest.session_id == session_id)
        )
        db.add(OrderChangeRequest(session_id=session_id, **fields))
        await db.commit()


async def _load(session_id: str) -> OrderChangeRequest | None:
    """The live pending change for this session, or None once it has expired."""
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(OrderChangeRequest).where(OrderChangeRequest.session_id == session_id)
            )
        ).scalar_one_or_none()

        if row is None:
            return None

        created = row.created_at
        if created is not None:
            # Postgres hands back an aware datetime, SQLite a naive one.
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created > timedelta(
                minutes=settings.SUPPORT_CHANGE_TTL_MINUTES
            ):
                await db.execute(
                    delete(OrderChangeRequest).where(OrderChangeRequest.id == row.id)
                )
                await db.commit()
                return None

        db.expunge(row)
        return row


async def _forget(session_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(OrderChangeRequest).where(OrderChangeRequest.session_id == session_id)
        )
        await db.commit()


async def _bump_attempts(session_id: str) -> int:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(OrderChangeRequest).where(OrderChangeRequest.session_id == session_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return settings.SUPPORT_CHANGE_MAX_ATTEMPTS
        row.attempts += 1
        await db.commit()
        return row.attempts


# ── Normalising answers ────────────────────────────────────────────────────


def _norm_postcode(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _norm_money(value: str) -> str:
    """Strip symbols, thousands separators and currency codes, keep the number."""
    digits = re.sub(r"[^0-9.]", "", (value or ""))
    try:
        return f"{float(digits):.2f}"
    except ValueError:
        return ""


def _challenge_for(node: dict) -> tuple[str, str, str]:
    """Pick what to ask, and what the right answer is. Never leaks the answer."""
    address = node.get("shippingAddress") or {}
    postcode = (address.get("zip") or "").strip()
    if postcode:
        return (
            "postcode",
            _norm_postcode(postcode),
            "the postcode on the delivery address for that order",
        )

    money = node["totalPriceSet"]["shopMoney"]
    return (
        "total",
        _norm_money(money["amount"]),
        f"the order total, in {money['currencyCode']}",
    )


def _answer_matches(row, given: str) -> bool:
    normalised = _norm_postcode(given) if row.challenge_kind == "postcode" else _norm_money(given)
    return bool(normalised) and _hash(normalised) == row.challenge_hash


# ── Eligibility ────────────────────────────────────────────────────────────


def _days_old(created_at: str) -> float:
    from datetime import datetime, timezone

    try:
        placed = datetime.fromisoformat((created_at or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - placed).total_seconds() / 86400


# Both writes - cancelling and re-addressing - need the same Shopify scope. If
# it is absent the whole flow is theatre, so it is checked before the shopper is
# asked for anything.
WRITE_SCOPE = "write_orders"

CANNOT_CHANGE = {
    "found": True,
    "eligible": False,
    "reason": "changes_not_enabled",
    "tell_customer": (
        "I am not able to change orders from this chat. Let me pass you to someone on "
        "the team who can sort it out for you."
    ),
}


def _eligibility(node: dict, action: str) -> dict | None:
    """None when the change can go ahead, otherwise why it cannot."""
    if node.get("cancelledAt"):
        return {
            "reason": "already_cancelled",
            "tell_customer": "That order has already been cancelled.",
        }

    status = (node.get("displayFulfillmentStatus") or "").upper()
    if status not in UNSTARTED:
        if action == CANCEL:
            return {
                "reason": "already_fulfilled",
                "tell_customer": (
                    "That order has already been packed and sent, so it is too late to "
                    "cancel it. You can send it back once it arrives."
                ),
            }
        return {
            "reason": "already_fulfilled",
            "tell_customer": (
                "That order is already on its way, so the address cannot be changed now. "
                "The carrier may still be able to redirect it."
            ),
        }

    if action == CANCEL:
        window = settings.SUPPORT_CANCEL_WINDOW_DAYS
        if window and _days_old(node.get("createdAt")) > window:
            return {
                "reason": "outside_window",
                "tell_customer": (
                    f"That order was placed more than {window} days ago, so I cannot cancel "
                    "it here. Let me put you through to someone who can help."
                ),
            }

    return None


# ── Step one ───────────────────────────────────────────────────────────────


async def begin(order_number: str, email: str, action: str, session_id: str | None = None) -> dict:
    """Match the order, check the change is possible, and record it against the chat.

    Returns what the agent needs to run the conversation - the order summary, the
    reasons to offer, and the question to put to the shopper - and nothing that
    would help someone who has not got the order in front of them.
    """
    if not await shopify_client.can(WRITE_SCOPE):
        logger.warning("Order changes are on, but the token has no %s scope", WRITE_SCOPE)
        return dict(CANNOT_CHANGE)

    if not settings.SUPPORT_ORDER_CHANGES:
        return {
            "available": False,
            "tell_customer": (
                "I cannot change orders from here. I can pass this to the team, who can."
            ),
        }

    if action not in ACTIONS:
        return {"error": "unknown_action", "actions": list(ACTIONS)}

    given_email = (email or "").strip().casefold()
    if not given_email or "@" not in given_email:
        return {"found": False, "reason": "email_required"}

    node = None
    for candidate in order_number_variants(order_number):
        data = await graphql(ORDER_FOR_CHANGE, {"query": f"name:{candidate}"})
        nodes = data["orders"]["nodes"]
        if nodes:
            node = nodes[0]
            break

    # Same answer for a wrong email as for an order that is not there, so this
    # cannot be used to find out which order numbers exist.
    if node is None or (node.get("email") or "").strip().casefold() != given_email:
        return {"found": False, "reason": "no_match"}

    blocked = _eligibility(node, action)
    if blocked:
        return {"found": True, "eligible": False, "order_number": node["name"], **blocked}

    if not session_id:
        # Without a session there is nowhere to hang the pending change, and the
        # agent must not be handed a secret it cannot keep.
        logger.error("Order change started with no chat session bound")
        return {
            "found": True,
            "eligible": False,
            "reason": "no_session",
            "tell_customer": (
                "I cannot take that through from here just now. Please contact us and we "
                "will do it for you."
            ),
        }

    kind, answer, phrasing = _challenge_for(node)
    await _save(
        session_id,
        order_gid=node["id"],
        order_name=node["name"],
        action=action,
        email=given_email,
        challenge_kind=kind,
        challenge_hash=_hash(answer),
        current_address=node.get("shippingAddress") or {},
        order_note=node.get("note"),
    )

    money = node["totalPriceSet"]["shopMoney"]
    address = node.get("shippingAddress") or {}
    return {
        "found": True,
        "eligible": True,
        # Deliberately no token: the change is held against this conversation, so
        # there is nothing for the agent to carry, lose or invent.
        "action": action,
        "order_number": node["name"],
        "placed_on": (node.get("createdAt") or "")[:10] or None,
        "total": round(float(money["amount"]), 2),
        "currency": money["currencyCode"],
        "items": [
            {"title": li["title"], "quantity": li["quantity"]}
            for li in node["lineItems"]["nodes"]
        ],
        # Enough for the shopper to recognise the address without printing it in
        # full at someone who may not be entitled to see it.
        "delivering_to": ", ".join(
            p for p in [address.get("city"), address.get("countryCodeV2")] if p
        )
        or None,
        "verification_required": settings.SUPPORT_VERIFY_ORDER_CHANGES,
        "ask_shopper_for": phrasing if settings.SUPPORT_VERIFY_ORDER_CHANGES else None,
        "reasons": CANCEL_REASONS if action == CANCEL else ADDRESS_REASONS,
        "expires_in_minutes": settings.SUPPORT_CHANGE_TTL_MINUTES,
    }


# ── Step two ───────────────────────────────────────────────────────────────


def _reason_note(action: str, reason_code: str, reason_text: str) -> str:
    catalogue = CANCEL_REASONS if action == CANCEL else ADDRESS_REASONS
    label = next((r["label"] for r in catalogue if r["code"] == reason_code), None)

    said = (reason_text or "").strip()[:REASON_NOTE_LIMIT]
    parts = [p for p in [label, f'"{said}"' if said else None] if p]
    return " - ".join(parts) or "No reason given"


def _address_input(row, new_address: dict) -> dict:
    """Build a MailingAddressInput, keeping whatever the shopper did not change.

    Country and province stay as they were unless a new code is supplied: a
    shopper correcting a street name should not silently move their parcel to
    another country because a field arrived empty.
    """
    current = row.current_address or {}
    given = {k: (v or "").strip() for k, v in (new_address or {}).items() if v}

    address = {
        "address1": given.get("address1") or current.get("address1"),
        "address2": given.get("address2", current.get("address2")),
        "city": given.get("city") or current.get("city"),
        "zip": given.get("zip") or current.get("zip"),
        "firstName": given.get("first_name") or current.get("firstName"),
        "lastName": given.get("last_name") or current.get("lastName"),
        "phone": given.get("phone") or current.get("phone"),
        "provinceCode": given.get("province_code") or current.get("provinceCode"),
        "countryCode": given.get("country_code") or current.get("countryCodeV2"),
    }
    return {k: v for k, v in address.items() if v}


async def commit(
    verification_answer: str = "",
    reason_code: str = "",
    reason_text: str = "",
    new_address: dict | None = None,
    session_id: str | None = None,
) -> dict:
    """Verify the pending change for this conversation, then carry it out."""
    if not session_id:
        return {"done": False, "reason": "no_session", "tell_customer": NO_PENDING}

    row = await _load(session_id)
    if row is None:
        return {
            "done": False,
            "reason": "nothing_pending",
            "tell_customer": NO_PENDING,
        }

    if settings.SUPPORT_VERIFY_ORDER_CHANGES and not row.verified:
        if not _answer_matches(row, verification_answer):
            attempts = await _bump_attempts(session_id)
            left = settings.SUPPORT_CHANGE_MAX_ATTEMPTS - attempts
            if left <= 0:
                await _forget(session_id)
                logger.warning("Order-change request dropped after too many attempts")
                return {
                    "done": False,
                    "reason": "too_many_attempts",
                    "tell_customer": (
                        "That did not match what we have on the order, so I have stopped "
                        "there. Please contact us and we will sort it out properly."
                    ),
                }
            return {
                "done": False,
                "reason": "verification_failed",
                "attempts_left": left,
                "tell_customer": "That does not match what we have on the order - try again?",
            }

    if reason_code and reason_code not in _REASON_CODES:
        return {"done": False, "reason": "unknown_reason_code", "reasons": CANCEL_REASONS}

    note = _reason_note(row.action, reason_code, reason_text)

    try:
        if row.action == CANCEL:
            result = await _do_cancel(row, note)
        else:
            result = await _do_address(row, note, new_address or {})
    except (ShopifyError, KeyError, ValueError) as exc:
        logger.warning("Order change %s failed for %s: %s", row.action, row.order_name, exc)
        return {
            "done": False,
            "reason": "store_error",
            "tell_customer": (
                "I could not put that through just now. Please try again in a minute, or "
                "contact us and we will do it for you."
            ),
        }

    # One pending change, one write. Spent either way, so a retry starts over.
    await _forget(session_id)
    return result


async def _do_cancel(row, note: str) -> dict:
    data = await graphql(
        ORDER_CANCEL,
        {
            "orderId": row.order_gid,
            "reason": SHOPIFY_CANCEL_REASON,
            "refundMethod": {"originalPaymentMethodsRefund": REFUND_ON_CANCEL},
            "restock": RESTOCK_ON_CANCEL,
            "staffNote": f"Cancelled by the shopper in chat. Reason: {note}",
            "notifyCustomer": NOTIFY_ON_CANCEL,
        },
    )
    payload = data["orderCancel"]
    errors = payload.get("orderCancelUserErrors") or []
    if errors:
        logger.warning("orderCancel rejected %s: %s", row.order_name, errors)
        return {
            "done": False,
            "reason": "rejected",
            "tell_customer": (
                "Our system would not let me cancel that one. Let me pass you to someone "
                "who can look at it properly."
            ),
        }

    return {
        "done": True,
        "action": CANCEL,
        "order_number": row.order_name,
        # Shopify cancels asynchronously; the refund follows the queue.
        "refund_started": REFUND_ON_CANCEL,
        "tell_customer": (
            f"Order {row.order_name} is cancelled. The refund goes back to your original "
            "payment method and usually lands within a few working days - you will get an "
            "email confirming it."
        ),
    }


async def _do_address(row, note: str, new_address: dict) -> dict:
    address = _address_input(row, new_address)
    if not address.get("address1"):
        return {
            "done": False,
            "reason": "address_required",
            "tell_customer": "I need the new address before I can move it.",
        }

    existing = (row.order_note or "").strip()
    trail = f"Delivery address changed by the shopper in chat. Reason: {note}"

    data = await graphql(
        ORDER_UPDATE_ADDRESS,
        {
            "input": {
                "id": row.order_gid,
                "shippingAddress": address,
                "note": f"{existing}\n{trail}".strip(),
            }
        },
    )
    payload = data["orderUpdate"]
    errors = payload.get("userErrors") or []
    if errors:
        logger.warning("orderUpdate rejected %s: %s", row.order_name, errors)
        return {
            "done": False,
            "reason": "rejected",
            "message": "; ".join(e.get("message", "") for e in errors),
            "tell_customer": (
                "That address was not accepted - check the street and postcode and I will "
                "try again."
            ),
        }

    saved = (payload.get("order") or {}).get("shippingAddress") or {}
    return {
        "done": True,
        "action": CHANGE_ADDRESS,
        "order_number": row.order_name,
        "new_address": ", ".join(
            p
            for p in [
                saved.get("address1"),
                saved.get("address2"),
                saved.get("city"),
                saved.get("zip"),
            ]
            if p
        ),
        "tell_customer": (
            f"Done - order {row.order_name} will now go to the new address. It has not "
            "shipped yet, so nothing else changes."
        ),
    }
