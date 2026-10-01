"""Why a chat turn failed, said two ways: kindly to the shopper, exactly to the owner.

A shopper whose answer failed because the AI account ran out of credit was told
"something went wrong, try again" - and trying again could never work. The
failure is now sorted into what it really was. The shopper gets an honest line
and what still works without the AI (collections, the size finder); the owner
sees the precise reason on the review page, with the AI account's balance.
"""

import asyncio
import logging
import time

import httpx

from app.core.config import settings
from app.services.shopify_client import ShopifyError

logger = logging.getLogger(__name__)

_last_failure: dict | None = None

SHOPPER_MESSAGES = {
    "ai_unavailable": ("Our shopping assistant is taking a short break right now. You can still "
                       "browse our collections below, or find the right size with the size finder."),
    "ai_busy": "The assistant is very busy at the moment - please try again in a minute.",
    "store_unreachable": "We can't reach the shop's catalogue just now - please try again in a minute.",
    "unexpected": "Sorry - something went wrong. Please try again.",
}

OWNER_REASONS = {
    "ai_no_credit": "The AI account (DeepSeek) has run out of credit - top it up and chats work again.",
    "ai_key_rejected": "The AI account rejected its API key - check DEEPSEEK_API_KEY on Railway.",
    "ai_busy": "The AI service is rate-limiting or overloaded - usually clears by itself.",
    "ai_timeout": "The AI service did not answer in time.",
    "store_unreachable": "The Shopify store could not be reached or refused the request.",
    "unexpected": "An unexpected error - see the server logs.",
}


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status_code", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def classify(exc: BaseException) -> str:
    """The owner-facing kind of failure, following the exception chain."""
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        status = _status_of(current)
        name = type(current).__name__.lower()
        if status == 402 or "insufficient balance" in text or "insufficient_quota" in text:
            return "ai_no_credit"
        if status == 401 or "authentication" in name or "invalid api key" in text:
            return "ai_key_rejected"
        if status == 429 or "ratelimit" in name or "rate limit" in text or status in (500, 502, 503):
            return "ai_busy"
        if isinstance(current, (asyncio.TimeoutError, TimeoutError)) or "timeout" in name:
            return "ai_timeout"
        if isinstance(current, ShopifyError):
            return "store_unreachable"
        current = current.__cause__ or current.__context__
    return "unexpected"


def shopper_code(kind: str) -> str:
    if kind in ("ai_no_credit", "ai_key_rejected"):
        return "ai_unavailable"
    if kind in ("ai_busy", "ai_timeout"):
        return "ai_busy"
    return kind if kind in SHOPPER_MESSAGES else "unexpected"


def record(kind: str, exc: BaseException) -> None:
    global _last_failure
    _last_failure = {"kind": kind, "reason": OWNER_REASONS.get(kind, OWNER_REASONS["unexpected"]),
                     "detail": str(exc)[:300], "at": time.time()}


async def ai_status() -> dict:
    """Whether the AI account can answer, from its balance - which costs nothing."""
    base = (settings.DEEPSEEK_BASE_URL or "https://api.deepseek.com").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(f"{base}/user/balance",
                                   headers={"Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}"})
        if res.status_code == 401:
            return {"ok": False, "reason": OWNER_REASONS["ai_key_rejected"]}
        data = res.json()
        balances = data.get("balance_infos") or []
        shown = ", ".join(f'{b.get("total_balance")} {b.get("currency")}' for b in balances)
        if not data.get("is_available"):
            return {"ok": False, "reason": OWNER_REASONS["ai_no_credit"], "balance": shown}
        return {"ok": True, "balance": shown}
    except Exception as exc:  # noqa: BLE001 - a status check must never fail the page
        logger.warning("Could not read the AI account balance", exc_info=True)
        return {"ok": None, "reason": f"Could not check the AI account: {str(exc)[:120]}"}


def last_failure() -> dict | None:
    if not _last_failure:
        return None
    return {**_last_failure, "minutes_ago": round((time.time() - _last_failure["at"]) / 60)}
