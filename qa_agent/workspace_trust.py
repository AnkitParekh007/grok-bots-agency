"""Persistent minimum server profile. This does not classify arbitrary operator writes.

An authenticated store cannot later be served anonymously; a shared store cannot
be reopened under a local profile. HTTP/tool/job checks also fence older processes.
"""
import sqlite3
import time

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .util import now


def level(mode, shared):
    if shared:
        if mode != "oidc":
            raise ValueError("shared workspace trust requires OIDC")
        return 2
    if mode not in ("demo", "local", "oidc"):
        raise ValueError("unknown workspace security profile")
    return 0 if mode == "demo" else 1


def _legacy_minimum(store):
    """Bounded metadata inference; uncertain/incomplete history requires shared auth."""
    deadline, steps = time.monotonic() + 1, 0
    def progress():
        nonlocal steps
        steps += 1000
        return int(steps >= 2000000 or time.monotonic() > deadline)
    store.db.set_progress_handler(progress, 1000)
    try:
        queries = [
            "SELECT CASE WHEN auth_mode='oidc' THEN 2 WHEN identity IS NOT NULL OR auth_mode='local' THEN 1 ELSE 0 END AS n FROM approvals LIMIT 1001",
            "SELECT 1 AS n FROM runs LIMIT 1001",
            "SELECT CASE WHEN tool LIKE '%oidc%' THEN 2 ELSE 1 END AS n FROM audit LIMIT 1001"]
        optional = {
            "web_sessions": "SELECT CASE WHEN issuer IS NOT NULL OR subject IS NOT NULL THEN 2 ELSE 1 END AS n FROM web_sessions LIMIT 1001",
            "oidc_flows": "SELECT 2 AS n FROM oidc_flows LIMIT 1001",
            "connection_discovery": "SELECT 1 AS n FROM connection_discovery LIMIT 1001",
            "connection_scopes": "SELECT 1 AS n FROM connection_scopes LIMIT 1001",
            "regression_versions": "SELECT 1 AS n FROM regression_versions LIMIT 1001",
            "jira_writes": "SELECT 1 AS n FROM jira_writes LIMIT 1001"}
        for table, sql in optional.items():
            if store.q("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)):
                queries.append(sql)
        required = 0
        for sql in queries:
            rows = store.q(sql)
            if len(rows) > 1000:
                return 2
            required = max(required, max((r["n"] for r in rows), default=0))
            if required == 2:
                return 2
        return required
    except sqlite3.Error:
        return 2
    finally:
        store.db.set_progress_handler(None, 0)


def register(agent, rank):
    """Monotonic, audited profile registration, completed before app admission."""
    if type(rank) is not int or not 0 <= rank <= 2:
        raise ValueError("invalid workspace trust level")
    store = agent.store
    with store.lock:
        if store.db.in_transaction:
            raise ValueError("workspace profile registration requires its own transaction")
        store.db.execute("BEGIN IMMEDIATE")
        try:
            rows = store.q("SELECT minimum_level FROM workspace_trust WHERE id=1")
            previous = rows[0]["minimum_level"] if rows else _legacy_minimum(store)
            required = max(previous, rank)
            stamp = now()
            if not rows:
                store.x("INSERT INTO workspace_trust VALUES(1,?,?,?)", (required, stamp, stamp))
            elif required > previous:
                store.x("UPDATE workspace_trust SET minimum_level=?,updated=? WHERE id=1", (required, stamp))
            if not rows or required > previous:
                agent.gateway.record(None, "workspace.profile", {"previous": previous, "requested": rank, "minimum": required},
                                     "allow" if rank >= required else "deny", "persistent minimum security profile")
            store.db.commit()
        except BaseException:
            store.db.rollback()
            raise
    agent.gateway.audit_delivery.flush()
    if rank < required:
        raise ValueError("workspace security profile downgrade refused; use an authenticated/shared profile or a new clean demo directory")
    return rank


def allowed(store, rank):
    try:
        rows = store.q("SELECT minimum_level FROM workspace_trust WHERE id=1")
        return bool(rows and type(rows[0]["minimum_level"]) is int and rank >= rows[0]["minimum_level"])
    except (sqlite3.Error, ValueError, RuntimeError):
        return False


class ProfileChanged(RuntimeError):
    pass


class ProfileFence:
    """Check before dispatch and every response chunk, including file downloads."""
    def __init__(self, app, *, store, rank):
        self.app, self.store, self.rank = app, store, rank

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        async def permitted():
            return await run_in_threadpool(allowed, self.store, self.rank)
        def unavailable():
            return JSONResponse({"detail": "workspace security profile changed; restart with its required profile"}, 503,
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                         "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
        if not await permitted():
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            return await unavailable()(scope, receive, send)
        if scope["type"] == "websocket":
            closed = False
            async def fenced_socket(message):
                nonlocal closed
                if closed:
                    return
                if message["type"] in ("websocket.accept", "websocket.send") and not await permitted():
                    closed = True
                    return await send({"type": "websocket.close", "code": 1008})
                await send(message)
            return await self.app(scope, receive, fenced_socket)
        # Starlette's sendfile extension bypasses body callbacks; force bounded chunks.
        scope = {**scope, "extensions": {k: v for k, v in scope.get("extensions", {}).items() if k != "http.response.pathsend"}}
        started, stopped = False, False
        async def fenced_send(message):
            nonlocal started, stopped
            if stopped:
                return
            if message["type"] in ("http.response.start", "http.response.body") and not await permitted():
                stopped = True
                if started:
                    raise ProfileChanged("workspace security profile changed during response")
                return await unavailable()(scope, receive, send)
            if message["type"] == "http.response.start":
                started = True
            await send(message)
        await self.app(scope, receive, fenced_send)
