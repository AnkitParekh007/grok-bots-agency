"""Runtime queue/restart and web caller lifecycle contracts, using offline sources."""
import json
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from qa_agent.auth import UserStore
from qa_agent.jobs import JobConflict
from qa_agent.runtime import QAAgent
from qa_agent.server import create_app
from tests.b1_helpers import agent_cfg, wait_status

pytestmark = [pytest.mark.execution, pytest.mark.security, pytest.mark.approval]


def queued_cfg(tmp_path, **job_options):
    cfg = agent_cfg("replay", jobs={"enabled": True, **job_options})
    cfg["oracles_dir"] = str(tmp_path / "missing-oracles")
    return cfg


def job_for(agent, rid, kind="planning"):
    return agent.store.q("SELECT * FROM jobs WHERE run_id=? AND kind=?", (rid, kind))[0]


def test_restart_claims_queued_planning_and_restores_selected_model(tmp_path):
    cfg = queued_cfg(tmp_path)
    first = QAAgent(tmp_path / "state", agent_cfg=cfg)
    second = None
    try:
        with patch.object(first.queue.workers, "wake"):
            rid = first.start_run("Review PCC-DEMO-1", story_key="PCC-DEMO-1", requested_by="owner")
        assert first.store.get_run(rid)["status"] == "queued_planning"
        assert not first._run_models
        second = QAAgent(tmp_path / "state", agent_cfg=cfg)
        second.recover_pending()
        assert second.queue.workers.wait(job_for(second, rid)["id"], timeout=20) == "completed"
        run = second.store.get_run(rid)
        assert run["status"] == "analysis_only" and run["model_provider"] == "replay"
        assert len(run["ac_json"]["acceptance_criteria"]) == 5
        assert job_for(second, rid)["attempts"] == 1
    finally:
        for agent in (first, second):
            if agent:
                agent.close();agent.store.db.close()


def test_another_agent_open_does_not_interrupt_owned_work(tmp_path):
    cfg = queued_cfg(tmp_path)
    first = QAAgent(tmp_path / "state", agent_cfg=cfg)
    second = None
    entered, release = threading.Event(), threading.Event()
    original = first.tracker.get_issue
    def slow_read(key):
        entered.set()
        assert release.wait(10)
        return original(key)
    try:
        with patch.object(first.tracker, "get_issue", slow_read):
            rid = first.start_run("Review", story_key="PCC-DEMO-1")
            assert entered.wait(10)
            second = QAAgent(tmp_path / "state", agent_cfg=cfg)
            assert second.store.get_run(rid)["status"] == "planning"
            second.recover_pending()
            release.set()
            assert first.queue.workers.wait(job_for(first, rid)["id"], timeout=20) == "completed"
        assert second.store.get_run(rid)["status"] == "analysis_only"
        assert job_for(second, rid)["attempts"] == 1
    finally:
        release.set()
        for agent in (first, second):
            if agent:
                agent.close();agent.store.db.close()


def test_admission_failure_rolls_back_run_event_audit_and_cache(tmp_path):
    a = QAAgent(tmp_path / "state", agent_cfg=queued_cfg(tmp_path, max_queued=1))
    try:
        with patch.object(a.queue.workers, "wake"):
            a.start_run("First", story_key="PCC-DEMO-1")
            baseline = {table: a.store.q(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] for table in ("runs", "audit", "events", "jobs")}
            with pytest.raises(JobConflict, match="capacity"):
                a.start_run("Second", story_key="PCC-DEMO-1")
        assert not a._run_models
        assert baseline == {table: a.store.q(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] for table in baseline}
    finally:
        a.close();a.store.db.close()


@pytest.mark.parametrize("action", ["logout", "rotate", "disable"])
def test_queued_web_request_is_denied_after_original_credential_revocation(tmp_path, action):
    cfg = queued_cfg(tmp_path)
    users = UserStore(tmp_path / "users.json")
    token = users.add("owner", "requester")
    app = create_app(tmp_path / "state", agent_cfg=cfg, auth_mode="local", users_file=users.path)
    a = app.state.agent
    try:
        with TestClient(app) as c:
            assert c.post("/api/login", json={"token": token}).status_code == 200
            header = {"X-CSRF-Token": c.cookies.get("qa_csrf")}
            with patch.object(a.queue.workers, "wake"):
                r = c.post("/api/chat", json={"message": "Review PCC-DEMO-1", "story_key": "PCC-DEMO-1"}, headers=header)
                assert r.status_code == 200, r.text
                rid = r.json()["run_id"]
            payload = job_for(a, rid)["payload"]
            assert token not in payload and c.cookies.get("qa_session") not in payload
            if action == "logout":
                assert c.post("/api/logout", headers=header).status_code == 200
            elif action == "rotate":
                users.add("owner", "requester")
            else:
                users.disable("owner")
            with patch.object(a.tracker, "get_issue") as read, patch.object(a.model, "generate") as infer:
                a.recover_pending()
                assert a.queue.workers.wait(job_for(a, rid)["id"], timeout=15) == "blocked"
                read.assert_not_called();infer.assert_not_called()
            assert a.store.get_run(rid)["status"] == "blocked"
    finally:
        a.close();a.store.db.close()


def test_queued_bearer_request_cannot_survive_token_rotation(tmp_path):
    users = UserStore(tmp_path / "users.json")
    token = users.add("owner", "requester")
    app = create_app(tmp_path / "state", agent_cfg=queued_cfg(tmp_path), auth_mode="local", users_file=users.path)
    a = app.state.agent
    try:
        with TestClient(app) as c:
            with patch.object(a.queue.workers, "wake"):
                r = c.post("/api/chat", json={"message": "Review", "story_key": "PCC-DEMO-1"}, headers={"Authorization": "Bearer " + token})
                assert r.status_code == 200
            rid = r.json()["run_id"]
            assert token not in job_for(a, rid)["payload"]
            users.add("owner", "requester")
            with patch.object(a.tracker, "get_issue") as read:
                a.recover_pending()
                assert a.queue.workers.wait(job_for(a, rid)["id"], timeout=15) == "blocked"
                read.assert_not_called()
    finally:
        a.close();a.store.db.close()


def test_cancel_queued_job_and_changed_configuration_never_replay(tmp_path):
    cfg = queued_cfg(tmp_path)
    a = QAAgent(tmp_path / "state", agent_cfg=cfg)
    other = None
    try:
        with patch.object(a.queue.workers, "wake"):
            cancelled = a.start_run("Cancel", story_key="PCC-DEMO-1")
            stale = a.start_run("Old revision", story_key="PCC-DEMO-1")
        a.cancel(cancelled, "owner")
        cfg["repair"]["max_format_repairs"] = 1
        other = QAAgent(tmp_path / "state", agent_cfg=cfg)
        with patch.object(other.tracker, "get_issue") as read:
            other.recover_pending()
            read.assert_not_called()
        assert other.store.get_run(cancelled)["status"] == "cancelled"
        assert other.store.get_run(stale)["status"] == "blocked"
        assert job_for(other, stale)["attempts"] == 0
    finally:
        for agent in (a, other):
            if agent:
                agent.close();agent.store.db.close()


def test_shared_profile_cannot_opt_out_of_durable_workers(tmp_path):
    cfg = agent_cfg("replay", deployment={"profile": "shared"}, jobs={"enabled": False})
    with pytest.raises(ValueError, match="durable"):
        QAAgent(tmp_path / "state", agent_cfg=cfg)


def test_execution_admission_failure_rolls_back_human_approval(tmp_path):
    a = QAAgent(tmp_path / "state", agent_cfg=agent_cfg("replay", jobs={"enabled": True, "max_queued": 1}))
    try:
        rid = a.start_run("Plan PCC-DEMO-1", story_key="PCC-DEMO-1", requested_by="owner", wait=True)
        run = a.store.get_run(rid)
        assert run["status"] == "awaiting_approval", run.get("error")
        with patch.object(a.queue.workers, "wake"):
            a.start_run("Occupy queue", story_key="PCC-DEMO-1")
            with pytest.raises(JobConflict, match="capacity"):
                a.approve(rid, "reviewer", "approved", identity="reviewer", auth_mode="local", plan_hash=run["plan_hash"])
        assert a.approvals.latest(rid) is None
        assert a.store.get_run(rid)["status"] == "awaiting_approval"
        assert a.store.q("SELECT * FROM jobs WHERE run_id=? AND kind='execution'", (rid,)) == []
    finally:
        a.close();a.store.db.close()


def test_runtime_instances_serialize_execution_on_the_same_database_resource(tmp_path):
    cfg = queued_cfg(tmp_path, execution_workers=2)
    first = QAAgent(tmp_path / "state", agent_cfg=cfg)
    second = QAAgent(tmp_path / "state", agent_cfg=cfg)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute_first(rid):
        calls.append(rid);entered.set()
        assert release.wait(10)
        first.store.update_run(rid, status="completed")
    def execute_second(rid):
        calls.append(rid)
        second.store.update_run(rid, status="completed")
    try:
        for rid in ("resource-first", "resource-second"):
            first.store.create_run(rid, "Queue scheduling contract only", "synthetic-local", "replay", "REPLAYED")
        with patch.object(first, "_execute", execute_first), patch.object(second, "_execute", execute_second):
            first.execute("resource-first", wait=False)
            assert entered.wait(10)
            second.execute("resource-second", wait=False)
            assert second.store.q("SELECT state FROM jobs WHERE run_id='resource-second'")[0]["state"] == "queued"
            assert calls == ["resource-first"]
            release.set()
            assert first.queue.workers.wait(job_for(first, "resource-first", "execution")["id"], timeout=15) == "completed"
            assert second.queue.workers.wait(job_for(second, "resource-second", "execution")["id"], timeout=15) == "completed"
            assert calls == ["resource-first", "resource-second"]
    finally:
        release.set()
        for agent in (first, second):
            agent.close();agent.store.db.close()


def test_plan_expiring_while_queued_cannot_reach_executor(tmp_path):
    a = QAAgent(tmp_path / "state", agent_cfg=agent_cfg("replay", jobs={"enabled": True}))
    try:
        rid = a.start_run("Plan", story_key="PCC-DEMO-1", wait=True)
        run = a.store.get_run(rid)
        assert run["status"] == "awaiting_approval"
        with patch.object(a.queue.workers, "wake"):
            a.approve(rid, "reviewer", "approved", identity="reviewer", auth_mode="local", plan_hash=run["plan_hash"])
        a.store.update_run(rid, plan_created_at="2020-01-01T00:00:00+00:00")
        with patch("qa_agent.runtime.PlaywrightExecutor.run") as execute:
            a.recover_pending()
            assert a.queue.workers.wait(job_for(a, rid, "execution")["id"], timeout=15) == "blocked"
            execute.assert_not_called()
        assert "expired before execution" in a.store.get_run(rid)["error"]
    finally:
        a.close();a.store.db.close()


def test_queued_execution_rechecks_the_authenticated_human_approver(tmp_path):
    users = UserStore(tmp_path / "users.json")
    tokens = {name: users.add(name, role) for name, role in (("owner", "requester"), ("reviewer", "approver"))}
    app = create_app(tmp_path / "state", agent_cfg=agent_cfg("replay", jobs={"enabled": True}), auth_mode="local", users_file=users.path)
    a = app.state.agent
    headers = lambda name: {"Authorization": "Bearer " + tokens[name]}
    try:
        with TestClient(app) as c:
            response = c.post("/api/chat", json={"message": "Plan", "story_key": "PCC-DEMO-1"}, headers=headers("owner"))
            assert response.status_code == 200
            rid = response.json()["run_id"]
            assert a.queue.workers.wait(job_for(a, rid)["id"], timeout=15) == "completed"
            run = a.store.get_run(rid)
            with patch.object(a.queue.workers, "wake"):
                approved = c.post(f"/api/runs/{rid}/approval", json={"decision": "approved", "plan_hash": run["plan_hash"]}, headers=headers("reviewer"))
                assert approved.status_code == 200, approved.text
            users.disable("reviewer")
            with patch.object(a, "_execute") as execute:
                a.recover_pending()
                assert a.queue.workers.wait(job_for(a, rid, "execution")["id"], timeout=15) == "blocked"
                execute.assert_not_called()
            response = c.get(f"/api/runs/{rid}", headers=headers("owner"))
            assert response.status_code == 200
            assert "reference" not in json.dumps(response.json().get("jobs"))
    finally:
        a.close();a.store.db.close()
