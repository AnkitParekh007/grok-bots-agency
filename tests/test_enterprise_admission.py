"""HTTP admission at the ASGI boundary, including untrusted chunked bodies."""
import asyncio
import pytest

from qa_agent.security import SecurityMiddleware, RateLimits
from qa_agent.store import Store

pytestmark = [pytest.mark.security]


def invoke(messages, *, limit=1024, method="POST", headers=None, capacity=False):
    sent, called = [], []
    async def app(scope, receive, send):
        called.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    middleware = SecurityMiddleware(app, max_body_bytes=limit, max_requests=1, shared=True)
    if capacity:
        middleware.slots.acquire()
    async def receive():
        return messages.pop(0)
    async def send(message):
        sent.append(message)
    asyncio.run(middleware({"type": "http", "method": method, "path": "/api/chat", "headers": headers or []}, receive, send))
    return sent, called


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_chunked_body_is_bounded_before_application_dispatch(method):
    sent, called = invoke([{"type": "http.request", "body": b"a" * 800, "more_body": True},
                           {"type": "http.request", "body": b"b" * 800, "more_body": False}], method=method)
    assert sent[0]["status"] == 413 and called == []
    headers = dict(sent[0]["headers"])
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-frame-options"] == b"DENY"


@pytest.mark.parametrize("length,status", [(b"2000", 413), (b"-1", 413), (b"n/a", 400)])
def test_declared_length_cannot_bypass_admission(length, status):
    sent, called = invoke([], headers=[(b"content-length", length)])
    assert sent[0]["status"] == status and not called


def test_capacity_and_exact_limit_success():
    sent, called = invoke([], capacity=True)
    assert sent[0]["status"] == 429 and not called
    sent, called = invoke([{"type": "http.request", "body": b"a" * 1024}])
    assert sent[0]["status"] == 200 and len(called[0]["body"]) == 1024


def test_authentication_limit_is_shared_across_limiter_instances(tmp_path):
    store = Store(tmp_path)
    try:
        first = RateLimits(store, clock=lambda: 1000)
        second = RateLimits(store, clock=lambda: 1000)
        assert first.consume("login", "client-one", 1) == 0
        assert second.consume("login", "client-one", 1) == 20
        assert second.consume("login", "client-two", 1) == 0
        assert "client-one" not in str(store.q("SELECT * FROM http_rate_limits"))
    finally:
        store.db.close()
