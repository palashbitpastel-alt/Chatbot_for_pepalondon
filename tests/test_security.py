"""The security controls, checked end to end against the real app.

Run:  python -m pytest tests -q      (no Shopify, AI or network needed)
"""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta

os.environ.update({
    "DATABASE_URL": "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"),
    "REVIEW_KEY": "test-admin-key-0123456789", "SHOPIFY_CLIENT_ID": "client", "SHOPIFY_CLIENT_SECRET": "secret",
    "SHOPIFY_STORE_URL": "default-shop.myshopify.com", "RATE_LIMIT_CHAT_PER_MINUTE": "3",
    "RATE_LIMIT_PER_MINUTE": "1000", "GUESS_MAX_FAILURES": "4", "API_DOCS": "false",
    "SUPPORT_CHAT_RETENTION_DAYS": "30",
})

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.core import security  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import ChatMessage, ShopperState  # noqa: E402
from app.db.session import AsyncSessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

KEY = {"X-Review-Key": os.environ["REVIEW_KEY"]}
STAFF = ["/api/v1/dashboard/summary", "/api/v1/dashboard/health", "/api/v1/dashboard/priority-actions",
         "/api/v1/analysis/finance", "/api/v1/chat/agents", "/api/v1/chat/memory",
         "/api/v1/chat/cs_someoneelse000000/history", "/api/v1/shopify/status",
         "/api/v1/review/conversations", "/api/v1/stores/list"]


def run(coro):
    return asyncio.get_event_loop_policy().get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _fresh():
    security.windows.clear()

    async def tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    run(tables())
    yield


def call(method, path, ip="1.1.1.1", **kw):
    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://test") as c:
            headers = {"X-Real-IP": ip, **kw.pop("headers", {})}
            return await c.request(method, path, headers=headers, **kw)
    return run(go())


@pytest.mark.parametrize("path", STAFF)
def test_staff_endpoints_need_the_key(path):
    assert call("GET", path).status_code == 401
    assert call("GET", path, headers={"X-Review-Key": "wrong"}).status_code == 401


def test_staff_chat_post_needs_the_key():
    assert call("POST", "/api/v1/chat", json={"message": "revenue?"}).status_code == 401
    assert call("POST", "/api/v1/chat/stream", json={"message": "revenue?"}).status_code == 401


def test_right_key_is_let_in():
    assert call("GET", "/api/v1/chat/agents", headers=KEY).status_code == 200
    assert call("GET", "/api/v1/stores/list", headers=KEY).status_code == 200


def test_wrong_keys_lock_the_caller_out_but_nobody_else():
    for _ in range(settings.GUESS_MAX_FAILURES):
        assert call("GET", "/api/v1/stores/list", ip="9.9.9.9", headers={"X-Review-Key": "guess"}).status_code == 401
    # Locked: even the right key is refused from that caller...
    assert call("GET", "/api/v1/stores/list", ip="9.9.9.9", headers=KEY).status_code == 429
    # ...while another caller with the right key still gets in.
    assert call("GET", "/api/v1/stores/list", ip="8.8.8.8", headers=KEY).status_code == 200


def test_chat_is_rate_limited_per_caller():
    codes = [call("POST", "/api/v1/chat", ip="5.5.5.5", json={"message": "x"}).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
    refused = call("POST", "/api/v1/support/chat", ip="5.5.5.5", json={"message": ""})
    assert refused.status_code == 429 and refused.headers["retry-after"] == "60"
    assert call("POST", "/api/v1/chat", ip="6.6.6.6", json={"message": "x"}).status_code == 401


def test_health_and_webhooks_are_never_rate_limited():
    for _ in range(20):
        assert call("GET", "/api/v1/health", ip="7.7.7.7").status_code == 200
    assert call("POST", "/api/v1/shopify/webhooks", content=b"{}").status_code == 401   # unsigned
    assert call("POST", "/api/v1/payments/stripe", content=b"{}").status_code == 400    # unsigned


def test_docs_are_closed():
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert call("GET", path).status_code == 404


def test_security_headers_on_every_response():
    for r in (call("GET", "/api/v1/health"), call("GET", "/api/v1/dashboard/summary"), call("GET", "/nope")):
        assert "max-age=" in r.headers["strict-transport-security"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"]


def test_staff_pages_cannot_be_framed():
    for path in ("/api/v1/review", "/api/v1/stores"):
        r = call("GET", path)
        assert r.status_code == 200 and r.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_cors_allows_storefronts_without_credentials():
    ask = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,x-shop-domain"}
    good = call("OPTIONS", "/api/v1/support/chat", headers={"Origin": "https://a-shop.myshopify.com", **ask})
    assert good.headers["access-control-allow-origin"] == "https://a-shop.myshopify.com"
    assert "access-control-allow-credentials" not in good.headers
    for origin in ("https://evil.example", "https://evil.myshopify.com.attacker.io"):
        bad = call("OPTIONS", "/api/v1/support/chat", headers={"Origin": origin, **ask})
        assert "access-control-allow-origin" not in bad.headers


def test_invalid_input_is_not_echoed_back():
    r = call("POST", "/api/v1/support/size", content=b'{"age": "<script>SECRET</script>"}',
             headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and "SECRET" not in r.text and "input" not in r.json()["detail"][0]


def test_unknown_shop_is_refused():
    assert call("GET", "/api/v1/support/widget-config?shop=not-installed.myshopify.com").status_code == 403
    assert call("GET", "/api/v1/support/widget-config").status_code == 200


def test_embedded_app_actions_need_a_shopify_token():
    for path in ("/api/v1/shopify/app/plan-request", "/api/v1/shopify/app/assistant", "/api/v1/shopify/app/settings",
                 "/api/v1/shopify/app/billing-portal", "/api/v1/shopify/app/auto-renew"):
        assert call("POST", path, json={}).status_code == 401
        assert call("POST", path, json={}, headers={"Authorization": "Bearer a.b.c"}).status_code == 401


def test_tokens_never_verify_without_a_client_secret(monkeypatch):
    import base64, hashlib, hmac, json, time
    from app.services import installs

    def token(secret):
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
        head, body = enc({"alg": "HS256"}), enc({"aud": settings.SHOPIFY_CLIENT_ID, "exp": time.time() + 60,
                                                 "nbf": time.time() - 5, "dest": "https://x.myshopify.com"})
        sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
        return f"{head}.{body}.{sig.decode().rstrip('=')}"

    assert installs.verify_id_token(token("secret")) == "x.myshopify.com"
    with pytest.raises(installs.InstallError):
        installs.verify_id_token(token("wrong-secret"))
    monkeypatch.setattr(settings, "SHOPIFY_CLIENT_SECRET", "")
    with pytest.raises(installs.InstallError):
        installs.verify_id_token(token(""))      # signed with the empty key: must still fail


def test_order_guessing_is_capped(monkeypatch):
    from app.services import shopify_storefront as sf
    asked = []

    async def graphql(query, variables=None):
        asked.append(variables)
        return {"orders": {"nodes": []}}
    monkeypatch.setattr(sf, "graphql", graphql)

    async def go():
        token = security._client_ip.set("3.3.3.3")
        try:
            results = [await sf.find_order("1001", "a@b.com") for _ in range(settings.GUESS_MAX_FAILURES + 2)]
        finally:
            security._client_ip.reset(token)
        return results
    results = run(go())
    assert [r["reason"] for r in results[:settings.GUESS_MAX_FAILURES]] == ["no_match"] * settings.GUESS_MAX_FAILURES
    assert results[-1]["reason"] == "too_many_attempts"
    before = len(asked)
    run(go())
    assert len(asked) == before          # locked callers never reach Shopify


def test_order_change_tries_are_counted_on_the_order_not_the_chat():
    key = "default-shop.myshopify.com|#1001"
    for _ in range(settings.GUESS_MAX_FAILURES):
        assert not security.guesses_blocked("order_change", key)
        security.wrong_guess("order_change", key)
    assert security.guesses_blocked("order_change", key)
    assert not security.guesses_blocked("order_change", "default-shop.myshopify.com|#1002")


def test_old_conversations_are_deleted_and_a_customer_can_be_forgotten():
    from sqlalchemy import select
    from app.services import retention

    async def go():
        old = datetime.utcnow() - timedelta(days=45)
        async with AsyncSessionLocal() as db:
            db.add_all([
                ChatMessage(session_id="cs_old0000000", role="user", content="hello", created_at=old),
                ChatMessage(session_id="cs_new0000000", role="user", content="my email is Jane@Example.com", shop="default-shop.myshopify.com"),
                ChatMessage(session_id="cs_new0000000", role="assistant", content="thanks", shop="default-shop.myshopify.com"),
                ChatMessage(session_id="cs_other00000", role="user", content="someone else", shop="default-shop.myshopify.com"),
                ChatMessage(session_id="staffthread01", role="user", content="old staff note", created_at=old),
                ShopperState(customer_key="customer:42", data={"k": 1}),
            ])
            await db.commit()
        gone = await retention.purge_old()
        async with AsyncSessionLocal() as db:
            left = set((await db.execute(select(ChatMessage.session_id))).scalars().all())
        assert gone["messages"] == 1 and left == {"cs_new0000000", "cs_other00000", "staffthread01"}
        forgotten = await retention.forget_customer("default-shop.myshopify.com", "jane@example.com", "42")
        async with AsyncSessionLocal() as db:
            left = set((await db.execute(select(ChatMessage.session_id))).scalars().all())
            state = (await db.execute(select(ShopperState))).scalars().all()
        assert forgotten == {"conversations": 1, "state": 1} and left == {"cs_other00000", "staffthread01"} and not state
    run(go())


def test_customer_data_export_needs_the_key_and_finds_their_conversations():
    from app.services import retention

    async def seed():
        async with AsyncSessionLocal() as db:
            db.add_all([ChatMessage(session_id="cs_export00000", role="user", content="I am sam@example.org, order 1001"),
                        ChatMessage(session_id="cs_export00000", role="assistant", content="Found it"),
                        ChatMessage(session_id="cs_notthem0000", role="user", content="nothing personal")])
            await db.commit()
    run(seed())
    path = "/api/v1/review/customer-data?email=Sam@Example.org"
    assert call("GET", path).status_code == 401
    found = call("GET", path, headers=KEY).json()
    assert [c["session_id"] for c in found["conversations"]] == ["cs_export00000"]
    assert len(found["conversations"][0]["messages"]) == 2
    run(retention.forget_customer("", "sam@example.org", None))
    assert call("GET", path, headers=KEY).json()["conversations"] == []


def _chat_events(monkeypatch, failures, tool_first=False):
    """Run one shopper turn against an agent that is busy `failures` times."""
    import json
    from app.api.v1.endpoints import support
    from app.agent.base import Agent

    class Busy(RuntimeError):
        status_code = 429

    state = {"calls": 0}

    async def stream(message, history):
        state["calls"] += 1
        if tool_first:
            yield {"type": "tool", "name": "add_to_cart", "phase": "start"}
        if state["calls"] <= failures:
            raise Busy("rate limited")
        yield {"type": "final", "reply": "Here you go."}

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(support, "CUSTOMER_SUPPORT_AGENT", Agent(
        name="customer_support", label="x", description="x", run=nothing, stream=stream))
    monkeypatch.setattr(settings, "SUPPORT_AI_BUSY_WAIT_SECONDS", 0)
    # No Shopify and no AI in tests: every lookup fails fast, as a shop that
    # cannot be reached would, and the turn must still be answered.
    from app.services import shopify_client

    async def no_token():
        raise shopify_client.ShopifyError("not configured in tests")
    monkeypatch.setattr(shopify_client, "access_token", no_token)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
    r = call("POST", "/api/v1/support/chat", ip="4.4.4.4", json={"message": "do you have coats"})
    events = [b.split("\n")[0][7:] for b in r.text.split("\n\n") if b.startswith("event: ")]
    error = next((json.loads(b.split("data: ", 1)[1]) for b in r.text.split("\n\n") if b.startswith("event: error")), None)
    return state["calls"], events, error


def test_a_busy_ai_is_asked_again_quietly(monkeypatch):
    calls, events, error = _chat_events(monkeypatch, failures=2)
    assert calls == 3 and error is None and "done" in events


def test_still_busy_gives_a_kind_line_and_something_to_do(monkeypatch):
    calls, events, error = _chat_events(monkeypatch, failures=99)
    assert calls == 1 + settings.SUPPORT_AI_BUSY_RETRIES
    assert error["code"] == "ai_busy" and error["offer_size"] is True and "very busy" not in error["message"]


def test_a_turn_that_already_used_a_tool_is_never_run_twice(monkeypatch):
    calls, events, error = _chat_events(monkeypatch, failures=99, tool_first=True)
    assert calls == 1 and error["code"] == "ai_busy"
