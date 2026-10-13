"""Who is calling, how often, and with which key.

Three things every public endpoint leans on:

- Rate limits. The storefront chat is public and every message costs several
  AI calls, so each caller (by IP) and each shop gets a number of requests per
  minute; guessing at the admin key, an order's email or its postcode is capped
  much harder. Counts are kept in memory, per server copy: with several copies
  a caller gets the limit on each, which is still a ceiling.
- The admin key. Staff pages and the staff API (dashboard, staff chat, the
  review and stores pages) take the X-Review-Key header; wrong guesses are
  counted per caller and locked out.
- Response headers every browser should get (HSTS, no sniffing, referrer).

Every number here is a setting (app/core/config.py); 0 switches that limit off.
"""

import hmac
import json
import logging
import time
from collections import deque
from contextvars import ContextVar

from fastapi import Header, HTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)

_client_ip: ContextVar[str] = ContextVar("client_ip", default="")

# Signed by the sender (Shopify, Stripe, Razorpay) or needed by the host: never limited.
_UNLIMITED = ("/api/v1/health", "/api/v1/shopify/webhooks", "/api/v1/payments/stripe", "/api/v1/payments/razorpay")
_CHAT = ("/api/v1/support/chat", "/api/v1/chat")

_HEADERS = [
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
]


def client_ip() -> str:
    """The caller of this request, as the host's proxy reported it."""
    return _client_ip.get()


class _Windows:
    """Sliding windows of timestamps: at most `limit` hits per `seconds` per key."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = {}
        self._swept = time.monotonic()

    def _sweep(self, now: float) -> None:
        # Callers that went away must not stay in memory for ever.
        if now - self._swept < 300:
            return
        self._swept = now
        for key in [k for k, q in self._hits.items() if not q or now - q[-1] > 3600]:
            del self._hits[key]

    def count(self, key: str, seconds: float) -> int:
        now = time.monotonic()
        q = self._hits.get(key)
        if not q:
            return 0
        while q and now - q[0] > seconds:
            q.popleft()
        return len(q)

    def hit(self, key: str, limit: int, seconds: float) -> bool:
        """Record one hit. False when it is over the limit (and not recorded)."""
        if limit <= 0:
            return True
        now = time.monotonic()
        self._sweep(now)
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def blocked(self, key: str, limit: int, seconds: float) -> bool:
        return limit > 0 and self.count(key, seconds) >= limit

    def clear(self) -> None:
        self._hits.clear()


windows = _Windows()


# ── Guessing: the admin key, an order's email, an order's postcode ──────────

def guesses_blocked(kind: str, who: str | None = None) -> bool:
    """Whether this caller (or this order) has used up its wrong guesses."""
    return windows.blocked(f"{kind}:{who or client_ip()}", settings.GUESS_MAX_FAILURES,
                           settings.GUESS_WINDOW_MINUTES * 60)


def wrong_guess(kind: str, who: str | None = None) -> None:
    key = f"{kind}:{who or client_ip()}"
    windows.hit(key, settings.GUESS_MAX_FAILURES, settings.GUESS_WINDOW_MINUTES * 60)
    if guesses_blocked(kind, who):
        logger.warning("Security: %s locked for %s minutes after repeated wrong guesses (%s)",
                       kind, settings.GUESS_WINDOW_MINUTES, who or client_ip() or "unknown caller")


def check_admin_key(key: str | None) -> None:
    """The staff key, compared in constant time; wrong guesses lock the caller out."""
    if not settings.REVIEW_KEY:
        raise HTTPException(status_code=503, detail="Set REVIEW_KEY on the server to open this page.")
    if guesses_blocked("admin_key"):
        raise HTTPException(status_code=429, detail="Too many wrong keys. Try again later.")
    if not key or not hmac.compare_digest(key.encode(), settings.REVIEW_KEY.encode()):
        wrong_guess("admin_key")
        raise HTTPException(status_code=401, detail="Wrong review key.")


async def require_admin(x_review_key: str | None = Header(default=None)) -> None:
    """Dependency for staff-only endpoints."""
    check_admin_key(x_review_key)


# ── The middleware ──────────────────────────────────────────────────────────

def _ip_of(scope: dict) -> str:
    wanted = (settings.CLIENT_IP_HEADER or "").strip().lower().encode()
    forwarded = b""
    for name, value in scope.get("headers") or []:
        if wanted and name == wanted:
            return value.decode("latin-1").split(",")[0].strip()
        if name == b"x-forwarded-for":
            forwarded = value
    if forwarded:
        return forwarded.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else ""


def _shop_of(scope: dict) -> str:
    for name, value in scope.get("headers") or []:
        if name == b"x-shop-domain":
            return value.decode("latin-1").strip().lower()[:255]
    query = scope.get("query_string", b"").decode("latin-1")
    for part in query.split("&"):
        if part.startswith("shop="):
            return part[5:].strip().lower()[:255]
    return ""


class SecurityMiddleware:
    """Pure ASGI: sets the caller for the request, refuses callers over their
    limit, and adds the security headers to every response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        ip = _ip_of(scope)
        token = _client_ip.set(ip)
        try:
            path = scope.get("path", "")
            if scope.get("method") != "OPTIONS" and not path.startswith(_UNLIMITED):
                over = None
                if not windows.hit(f"ip:{ip}", settings.RATE_LIMIT_PER_MINUTE, 60):
                    over = "requests"
                elif scope.get("method") == "POST" and path.startswith(_CHAT):
                    if not windows.hit(f"chat:{ip}", settings.RATE_LIMIT_CHAT_PER_MINUTE, 60):
                        over = "chat"
                    elif not windows.hit(f"shopchat:{_shop_of(scope)}", settings.RATE_LIMIT_SHOP_CHAT_PER_MINUTE, 60):
                        over = "shop chat"
                if over:
                    logger.warning("Security: rate limit (%s) reached by %s on %s", over, ip or "unknown caller", path)
                    return await self._refuse(send)

            async def with_headers(message):
                if message["type"] == "http.response.start":
                    have = {name for name, _ in message.get("headers") or []}
                    message = {**message, "headers": list(message.get("headers") or [])
                               + [(n, v) for n, v in _HEADERS if n not in have]}
                await send(message)

            await self.app(scope, receive, with_headers)
        finally:
            _client_ip.reset(token)

    @staticmethod
    async def _refuse(send) -> None:
        body = json.dumps({"detail": "Too many requests. Please try again in a minute."}).encode()
        await send({"type": "http.response.start", "status": 429,
                    "headers": [(b"content-type", b"application/json"), (b"retry-after", b"60"),
                                (b"access-control-allow-origin", b"*"), *_HEADERS]})
        await send({"type": "http.response.body", "body": body})
