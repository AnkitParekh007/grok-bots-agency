"""Bounded HTTP admission, security headers and persistent fixed-window limits."""
import hashlib
import asyncio
import threading
import time

from starlette.responses import JSONResponse


class RateLimits:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        store.x("CREATE TABLE IF NOT EXISTS http_rate_limits(scope TEXT, key_hash TEXT, window INTEGER, hits INTEGER, "
                "PRIMARY KEY(scope,key_hash))")

    def consume(self, scope, key, limit, period=60):
        if not 1 <= limit <= 100000 or not 1 <= period <= 3600:
            raise ValueError("invalid rate limit")
        now = int(self.clock());window = now // period
        hashed = hashlib.sha256(key.encode()).hexdigest()
        with self.store.lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.execute("DELETE FROM http_rate_limits WHERE window<?", (window - 2,))
                self.store.db.execute("INSERT INTO http_rate_limits VALUES(?,?,?,1) ON CONFLICT(scope,key_hash) "
                                      "DO UPDATE SET hits=CASE WHEN window=excluded.window THEN hits+1 ELSE 1 END, window=excluded.window",
                                      (scope, hashed, window))
                count = self.store.db.execute("SELECT hits FROM http_rate_limits WHERE scope=? AND key_hash=?", (scope, hashed)).fetchone()[0]
                self.store.db.commit()
            except Exception:
                self.store.db.rollback();raise
        return 0 if count <= limit else period - (now % period)


class SecurityMiddleware:
    """Prebuffer only bounded mutation bodies; no unlimited request.body() read."""
    def __init__(self, app, *, max_body_bytes=1_000_000, max_requests=40, body_timeout_s=15, shared=False):
        if not 1024 <= max_body_bytes <= 4_000_000 or not 1 <= max_requests <= 256:
            raise ValueError("invalid HTTP admission bounds")
        self.app, self.max_body = app, max_body_bytes
        self.slots = threading.BoundedSemaphore(max_requests)
        self.shared = shared
        if not 1 <= body_timeout_s <= 60:
            raise ValueError("invalid request body deadline")
        self.body_timeout_s = body_timeout_s

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send);return
        async def protected_send(message):
            if message["type"] == "http.response.start":
                h = list(message.get("headers", []))
                h += [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                      (b"x-frame-options", b"DENY"),
                      (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
                if scope.get("path", "").startswith("/api/"):
                    h.append((b"cache-control", b"no-store"))
                if self.shared:
                    h.append((b"strict-transport-security", b"max-age=31536000"))
                message = {**message, "headers": h}
            await send(message)
        if not self.slots.acquire(blocking=False):
            await JSONResponse({"detail": "service request capacity exceeded"}, 429, headers={"Retry-After": "1"})(scope, receive, protected_send)
            return
        try:
            if scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
                headers = dict(scope.get("headers", []))
                try:
                    length = int(headers.get(b"content-length", b"0"))
                except ValueError:
                    await JSONResponse({"detail": "invalid content length"}, 400)(scope, receive, protected_send);return
                if length < 0 or length > self.max_body:
                    await JSONResponse({"detail": "request body exceeds limit"}, 413)(scope, receive, protected_send);return
                body = bytearray()
                deadline = time.monotonic() + self.body_timeout_s
                while True:
                    try:
                        message = await asyncio.wait_for(receive(), max(0, deadline - time.monotonic()))
                    except TimeoutError:
                        await JSONResponse({"detail": "request body deadline exceeded"}, 408)(scope, receive, protected_send);return
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_body:
                        await JSONResponse({"detail": "request body exceeds limit"}, 413)(scope, receive, protected_send);return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
                replayed = False
                async def bounded_receive():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()
                receiver = bounded_receive
            else:
                receiver = receive

            await self.app(scope, receiver, protected_send)
        finally:
            self.slots.release()
