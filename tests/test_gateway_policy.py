import sqlite3
import pytest
from qa_agent.runtime import QAAgent
from qa_agent.gateway import PolicyDenied
from qa_agent.environments import EnvironmentRefused
from qa_agent import ROOT

pytestmark = [pytest.mark.security, pytest.mark.audit]

ENV = {"name": "synthetic-local", "base_url": "http://127.0.0.1:8601", "allowed_hosts": ["127.0.0.1", "localhost"]}


@pytest.fixture
def agent(tmp_path):
    return QAAgent(tmp_path)


def test_unlisted_tool_denied_and_audited(agent):
    with pytest.raises(PolicyDenied):
        agent.gateway.invoke("r1", "shell.exec", {"cmd": "rm -rf /"}, lambda: "ran")
    rows = agent.store.audit_rows("r1")
    assert rows[-1]["decision"] == "deny" and rows[-1]["outcome"] == "not_executed" and len(rows[-1]["args_hash"]) == 64


def test_jira_write_not_allowed(agent):
    with pytest.raises(PolicyDenied):
        agent.gateway.invoke("r1", "tracker.add_comment", {"key": "PCC-1"}, lambda: None)


def test_approval_required_tool_fails_closed(agent):
    with pytest.raises(PolicyDenied, match="approval"):
        agent.gateway.invoke("r2", "http.request", {"method": "GET", "url": "http://127.0.0.1:8601/"}, lambda: 1,
                             {"env": ENV, "plan_hash": "abc"})


def test_approval_bound_to_plan_hash(agent):
    agent.approvals.record("r3", "hash-A", "tester", "approved")
    ctx = {"env": ENV, "plan_hash": "hash-A"}
    assert agent.gateway.invoke("r3", "http.request", {"method": "GET", "url": "http://127.0.0.1:8601/x"}, lambda: "ok", ctx) == "ok"
    with pytest.raises(PolicyDenied):
        agent.gateway.invoke("r3", "http.request", {"method": "GET", "url": "http://127.0.0.1:8601/x"}, lambda: "ok",
                             {"env": ENV, "plan_hash": "hash-B"})


def test_non_environment_urls_denied(agent):
    agent.approvals.record("r4", "h", "tester", "approved")
    ctx = {"env": ENV, "plan_hash": "h"}
    for url in ["https://example.com/", "http://10.0.0.5:8601/", "http://127.0.0.1:9999/", "file:///etc/passwd"]:
        with pytest.raises(PolicyDenied):
            agent.gateway.invoke("r4", "browser.navigate", {"url": url}, lambda: None, ctx)
    with pytest.raises(PolicyDenied):
        agent.gateway.invoke("r4", "http.request", {"method": "DELETE", "url": "http://127.0.0.1:8601/x"}, lambda: None, ctx)


def test_file_outside_fixture_root_denied(agent):
    agent.approvals.record("r5", "h", "tester", "approved")
    with pytest.raises(PolicyDenied, match="file outside"):
        agent.gateway.invoke("r5", "browser.upload_file", {"url": "http://127.0.0.1:8601/", "file": "/etc/passwd"}, lambda: None,
                             {"env": ENV, "plan_hash": "h"})
    with pytest.raises(PolicyDenied, match="file outside"):
        agent.gateway.invoke("r5", "browser.upload_file",
                             {"url": "http://127.0.0.1:8601/", "file": str(ROOT / "fixtures/data/../../config/policy.json")},
                             lambda: None, {"env": ENV, "plan_hash": "h"})


def test_audit_is_append_only(agent):
    agent.gateway.invoke("r6", "env.resolve", {"name": "synthetic-local"}, lambda: 1)
    with pytest.raises(sqlite3.DatabaseError):
        agent.store.x("DELETE FROM audit")
    with pytest.raises(sqlite3.DatabaseError):
        agent.store.x("UPDATE audit SET decision='allow'")
    assert (agent.artifacts.path_for("r6", "audit.jsonl")).read_text().count("\n") == 1


def test_environment_registry_refuses_unknown_and_nonlocal(agent, monkeypatch):
    with pytest.raises(EnvironmentRefused):
        agent.envs.resolve("playground")
    with pytest.raises(EnvironmentRefused):
        agent.envs.resolve("production")
    monkeypatch.setenv("SYNTHETIC_SUT_URL", "http://pcc.example.com:8601")
    with pytest.raises(EnvironmentRefused):
        agent.envs.resolve("synthetic-local")
