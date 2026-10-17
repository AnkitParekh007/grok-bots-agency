"""Pagination cannot turn tenant/ownership filtering into partial or leaked pages."""
import base64
import sqlite3

import pytest
from fastapi.testclient import TestClient

from qa_agent.auth import Principal
from qa_agent.runtime import QAAgent
from qa_agent.server import create_app
from qa_agent.run_history import page_runs, HistoryUnavailable
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.security]


@pytest.fixture
def history(tmp_path):
    agent = QAAgent(tmp_path, agent_cfg=agent_cfg("replay"))
    agent.projects.clear()
    agent.projects.update({"A": {"tenant_id": "tenant-a"}, "B": {"tenant_id": "tenant-a"}, "C": {"tenant_id": "tenant-b"}})
    records = [(f"z{i:03}", "B", "bob", "tenant-a") for i in range(140)]
    records += [(f"o{i}", "A", "alice", "tenant-a") for i in range(4)]
    records += [("other", "A", "bob", "tenant-a"), ("foreign", "C", "alice", "tenant-b"),
                ("mismatch", "C", "alice", "tenant-a"), ("unscoped", None, "alice", "tenant-a")]
    with agent.store.lock:
        agent.store.db.execute("BEGIN IMMEDIATE")
        for rid, project, owner, tenant in records:
            agent.store.create_run(rid, "synthetic-private-" + owner, "synthetic-local", "replay", "Replay")
            agent.store.update_run(rid, project=project, requested_by=owner, tenant_id=tenant, status="completed")
            agent.store.x("UPDATE runs SET created='2026-10-17T10:00:00.000+00:00' WHERE id=?", (rid,))
        agent.store.db.commit()
    yield agent
    agent.close()
    agent.store.db.close()


def caller(**over):
    return Principal(**{"name": "alice", "role": "requester", "mode": "local", "tenant_id": "tenant-a",
                        "project_roles": {"A": "requester"}, **over})


def test_authorization_is_applied_before_limit_and_ties_are_stable(history, monkeypatch):
    a = history
    monkeypatch.setattr(a.store, "list_runs", lambda: pytest.fail("unbounded history used"))
    one = page_runs(a, caller(), limit=2)
    assert [r["id"] for r in one["items"]] == ["o3", "o2"]
    a.store.create_run("newer", "new", "synthetic-local", "replay", "Replay")
    a.store.update_run("newer", status="completed", project="A", tenant_id="tenant-a", requested_by="alice")
    two = page_runs(a, caller(), limit=2, cursor=one["next_cursor"])
    assert [r["id"] for r in two["items"]] == ["o1", "o0"]
    assert two["next_cursor"] is None


def test_project_approvers_and_legacy_global_scope_remain_explicit(history):
    scoped = page_runs(history, caller(role="approver", project_roles={"A": "approver"}))
    assert {r["id"] for r in scoped["items"]} == {"other", "o0", "o1", "o2", "o3"}
    global_owner = page_runs(history, caller(project_roles=None))
    assert {r["id"] for r in global_owner["items"]} == {"unscoped", "o0", "o1", "o2", "o3"}
    history.access.strict = True
    assert page_runs(history, caller(project_roles=None))["items"] == []


def test_forged_or_cross_principal_cursor_does_not_grant_scope(history):
    other_page = page_runs(history, caller(name="bob", project_roles={"B": "requester"}), limit=2)
    page = page_runs(history, caller(), limit=100, cursor=other_page["next_cursor"])
    assert {r["id"] for r in page["items"]} == {"o0", "o1", "o2", "o3"}
    assert not any("bob" in r["request"] for r in page["items"])
    assert page_runs(history, caller(tenant_id="tenant-b", project_roles={"A": "approver"}))["items"] == []


@pytest.mark.parametrize("limit", [0, -1, 101, 100000, True, 2.0, "2", None])
def test_invalid_page_bounds_fail_before_query(history, limit):
    with pytest.raises(ValueError): page_runs(history, caller(), limit=limit)


@pytest.mark.parametrize("cursor", ["!", "a", "A" * 513, "Li4vZXZpbA", "e30", "W251bGwsbnVsbF0", "W10", "=", 5])
def test_malformed_cursors_fail_closed(history, cursor):
    with pytest.raises(ValueError, match="cursor"): page_runs(history, caller(), cursor=cursor)


def test_cursor_fields_cannot_inject_sql(history):
    cursor = base64.urlsafe_b64encode(b'["2026-10-17T10:00:00+00:00","x\' OR 1=1 --"]').decode().rstrip("=")
    with pytest.raises(ValueError): page_runs(history, caller(), cursor=cursor)
    assert history.store.q("SELECT COUNT(*) AS n FROM runs")[0]["n"] == 148


def test_query_deadline_interrupts_and_does_not_poison_shared_database(history, monkeypatch):
    ticks = iter([0] + [3] * 100)
    interrupted, original = [], history.store.q
    def observe(*args):
        try:
            return original(*args)
        except sqlite3.OperationalError as error:
            interrupted.append(error.sqlite_errorcode)
            raise
    with monkeypatch.context() as patch:
        patch.setattr(history.store, 'q', observe)
        with pytest.raises(HistoryUnavailable): page_runs(history, caller(), clock=lambda: next(ticks))
    assert interrupted == [sqlite3.SQLITE_INTERRUPT]
    # Both reads and a subsequent write work after removal of the progress handler.
    assert history.store.q("SELECT COUNT(*) AS n FROM runs")[0]["n"] == 148
    history.store.update_run("o0", request="still writable")
    assert len(page_runs(history, caller())["items"]) == 4


def test_instruction_budget_stops_sparse_scope_scan_even_with_frozen_clock(history):
    history.store.x("""WITH RECURSIVE numbers(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM numbers WHERE n<300000)
        INSERT INTO runs(id,created,status,request,requested_by,project,tenant_id)
        SELECT printf('dense%06d',n),'2100-01-01T00:00:00+00:00','completed','synthetic','bob','B','tenant-a' FROM numbers""")
    with pytest.raises(HistoryUnavailable): page_runs(history, caller(), clock=lambda: 0)
    assert history.store.q("SELECT COUNT(*) AS n FROM runs")[0]['n'] == 300149


def test_bounded_api_retains_legacy_array_and_exposes_continuation(tmp_path):
    app = create_app(tmp_path, agent_cfg=agent_cfg("replay"), auth_mode="demo")
    agent = app.state.agent
    try:
        with agent.store.lock:
            agent.store.db.execute("BEGIN IMMEDIATE")
            for i in range(105):
                agent.store.create_run(f"r{i:03}", "synthetic", "synthetic-local", "replay", "Replay")
                agent.store.update_run(f"r{i:03}", status="completed")
            agent.store.db.commit()
        c = TestClient(app)
        legacy = c.get("/api/runs")
        assert len(legacy.json()) == 100 and legacy.headers["x-next-cursor"]
        page = c.get("/api/run-pages").json()
        assert page["items"] == legacy.json() and page["next_cursor"] == legacy.headers["x-next-cursor"]
        tail = c.get("/api/run-pages", params={"cursor": page["next_cursor"]}).json()
        assert len(tail["items"]) == 5 and tail["next_cursor"] is None
        assert not {r["id"] for r in page["items"]}.intersection(r["id"] for r in tail["items"])
        for limit in (0, 101): assert c.get("/api/run-pages", params={"limit": limit}).status_code == 422
        assert c.get("/api/run-pages", params={"cursor": "a"}).status_code == 400
        assert c.get("/api/runs", params={"cursor": "a"}).status_code == 400
        assert c.get("/api/run-pages").headers["cache-control"] == "no-store"
    finally:
        agent.close()
        agent.store.db.close()
