"""Bounded keyset pages; tenant/project/ownership filtering precedes LIMIT."""
import base64
import json
import re
import sqlite3
import time
from datetime import datetime

from .auth import has_role
from .util import canon


class HistoryUnavailable(RuntimeError):
    pass


def _decode(cursor):
    if cursor is None:
        return None, None
    if not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", cursor):
        raise ValueError("invalid run-history cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        values = json.loads(raw)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor or not isinstance(values, list) or len(values) != 2:
            raise ValueError()
        created, run_id = values
        if not isinstance(created, str) or len(created) > 80 or datetime.fromisoformat(created).tzinfo is None:
            raise ValueError()
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise ValueError()
        return created, run_id
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("invalid run-history cursor") from None


def page_runs(agent, principal, *, limit=100, cursor=None, clock=time.monotonic):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("run-history page limit must be 1 through 100")
    created, run_id = _decode(cursor)
    visible = sorted(k for k in agent.projects if agent.access.allows(principal, k))
    approvable = sorted(k for k in visible if agent.access.allows(principal, k, "approver"))
    if len(visible) > 1000:
        raise ValueError("run-history project inventory exceeds limit")
    global_scope = not agent.access.strict and principal.project_roles is None
    # Constant SQL and bound JSON arrays keep the authorization predicate inside
    # SQLite; unauthorized rows never fill a page or enter application memory.
    sql = """
        SELECT id,created,status,story_key,environment,model_label,request,requested_by,project,tenant_id
        FROM runs
        WHERE (?=1 OR (tenant_id=? AND (
          ((project IS NULL OR project='') AND ?=1 AND (?=1 OR requested_by=?)) OR
          (project IN (SELECT value FROM json_each(?)) AND
             (project IN (SELECT value FROM json_each(?)) OR requested_by=?))
        ))) AND (? IS NULL OR created<? OR (created=? AND id<?))
        ORDER BY created DESC,id DESC LIMIT ?
        """
    arguments = (int(principal.mode == "demo"), principal.tenant_id, int(global_scope), int(has_role(principal, "approver")),
              principal.name, canon(visible), canon(approvable), principal.name,
              created, created, created, run_id, limit + 1)
    with agent.store.lock:
        deadline, steps = clock() + 2, 0
        def budget():
            nonlocal steps
            steps += 1000
            return steps >= 2_000_000 or clock() >= deadline
        # The Store lock serializes this connection's handler with other SQL.
        # Always remove it, including on interruption, before releasing the lock.
        agent.store.db.set_progress_handler(budget, 1000)
        try:
            rows = agent.store.q(sql, arguments)
            if clock() >= deadline:
                raise HistoryUnavailable("run-history query budget exceeded")
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                raise HistoryUnavailable("run-history query budget exceeded") from None
            raise
        finally:
            agent.store.db.set_progress_handler(None, 0)
    more = len(rows) > limit
    rows = rows[:limit]
    following = None
    if more:
        last = rows[-1]
        following = base64.urlsafe_b64encode(canon([last["created"], last["id"]]).encode()).decode().rstrip("=")
    return {"items": rows, "next_cursor": following, "limit": limit}
