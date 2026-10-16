"""Authenticated discovery/scope and REST/MCP broker parity using synthetic transports."""
import copy
import json
import httpx
import pytest
from fastapi.testclient import TestClient

from qa_agent.auth import Principal, UserStore
from qa_agent.runtime import QAAgent
from qa_agent.server import create_app
from qa_agent.integrations.http import IntegrationNotAuthorized, IntegrationError
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.security]

ADMIN = Principal("admin", "approver", "local", "tenant-a", {"PCC": "approver"})


def connection(provider="bitbucket_cloud"):
    return {"provider": provider, "route": "token", "tenant_id": "tenant-a", "administrators": ["admin"],
            "enabled": True, "approved": True, "approval_ref": "synthetic-only", "discovery_approved": True,
            "discovery": {"owners": ["SYN"], "max_pages": 2}, "http": {
                "base_url": "https://source.example.invalid", "allowed_hosts": ["source.example.invalid"],
                "auth": {"type": "bearer", "token_env": "CONNECTION_TEST_KEY"}, "timeout_s": 2, "max_retries": 0}}


class Rest:
    def __init__(self):
        self.calls, self.next, self.private = [], None, False
    def __call__(self, request):
        self.calls.append(request)
        if request.url.path.endswith("/project/search"):
            return httpx.Response(200, json={"values": [{"key": "PCC", "name": "Synthetic Project"}], "isLast": True})
        if request.url.path.endswith("/2.0/repositories/SYN"):
            data = {"values": [{"full_name": "SYN/pim-import-service", "name": "Synthetic Repo"}, {"full_name": "SYN/other", "name": "Other Repo"}]}
            if self.next:
                data["next"] = self.next
            return httpx.Response(200, json=data)
        if request.url.path.endswith("/2.0/repositories/SYN/pim-import-service"):
            return httpx.Response(200, json={"full_name": "SYN/pim-import-service", "name": "Synthetic Repo", "mainbranch": {"name": "main"}})
        if "/issue/" in request.url.path:
            key = request.url.path.split("/issue/")[1]
            return httpx.Response(200, json={"key": key, "fields": {"project": {"key": "PCC"}, "summary": "Synthetic story", "description": "AC"}})
        raise AssertionError(str(request.url))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTION_TEST_KEY", "synthetic-secret-only")
    cfg = agent_cfg("replay")
    cfg["projects"]["PCC"].update(tenant_id="tenant-a", git_connection="bb")
    cfg["connections"] = {"bb": connection(), "jira": connection("jira_cloud")}
    agent = QAAgent(tmp_path, agent_cfg=cfg)
    rest = Rest()
    agent.connections.transports = {name: httpx.MockTransport(rest) for name in cfg["connections"]}
    yield agent, rest
    agent.connections.close();agent.store.db.close()


def test_discovery_does_not_grant_access_until_reviewed_selection(setup):
    agent, rest = setup
    with pytest.raises(IntegrationNotAuthorized, match="active reviewed"):
        agent.git_for("PCC")
    result = agent.connections.discover("bb", ADMIN)
    assert result["complete"] and len(result["resources"]) == 2
    assert not agent.connections.list(ADMIN)[0]["selected_resources"]
    selected = agent.connections.select("bb", ADMIN, result["receipt"], ["SYN/pim-import-service"])
    adapter = agent.git_for("PCC")
    assert adapter.get_repository("SYN/pim-import-service")["full_name"] == "SYN/pim-import-service"
    before = len(rest.calls)
    with pytest.raises(IntegrationError):
        adapter.get_repository("SYN/other")
    assert len(rest.calls) == before
    assert selected["revision"] == agent.connections.revision("PCC")["git"]["selection"]
    assert "synthetic-secret-only" not in json.dumps(agent.store.audit_rows(None))
    agent.connections.revoke("bb", ADMIN, selected["revision"])
    with pytest.raises(IntegrationNotAuthorized, match="active reviewed"):
        agent.git_for("PCC")


@pytest.mark.parametrize("principal", [Principal("someone", "approver", "local", "tenant-a"),
    Principal("admin", "approver", "local", "tenant-b"), Principal("admin", "requester", "local", "tenant-a"),
    Principal("admin", "approver", "demo", "tenant-a"), Principal("admin", "approver", "local", "tenant-a", kind="service")])
def test_connection_scope_requires_explicit_same_tenant_human_administrator(setup, principal):
    agent, rest = setup
    assert agent.connections.list(principal) == []
    with pytest.raises(IntegrationNotAuthorized):
        agent.connections.discover("bb", principal)
    assert rest.calls == []


def test_unknown_resource_and_replayed_or_stale_selection_are_denied(setup):
    agent, _ = setup
    first = agent.connections.discover("bb", ADMIN)
    with pytest.raises(IntegrationNotAuthorized, match="not in"):
        agent.connections.select("bb", ADMIN, first["receipt"], ["SYN/unknown"])
    chosen = agent.connections.select("bb", ADMIN, first["receipt"], ["SYN/pim-import-service"])
    with pytest.raises(IntegrationNotAuthorized, match="expired"):
        agent.connections.select("bb", ADMIN, first["receipt"], ["SYN/pim-import-service"], chosen["revision"])
    second = agent.connections.discover("bb", ADMIN)
    with pytest.raises(IntegrationError, match="scope changed"):
        agent.connections.select("bb", ADMIN, second["receipt"], ["SYN/other"])


def test_changed_operator_configuration_invalidates_discovery_and_scope(setup):
    agent, _ = setup
    first = agent.connections.discover("bb", ADMIN)
    agent.connections.cfg["bb"]["approval_ref"] = "changed"
    with pytest.raises(IntegrationNotAuthorized, match="changed"):
        agent.connections.select("bb", ADMIN, first["receipt"], ["SYN/pim-import-service"])
    fresh = agent.connections.discover("bb", ADMIN)
    agent.connections.select("bb", ADMIN, fresh["receipt"], ["SYN/pim-import-service"])
    agent.connections.cfg["bb"]["approval_ref"] = "changed-again"
    with pytest.raises(IntegrationNotAuthorized, match="active reviewed"):
        agent.git_for("PCC")


@pytest.mark.parametrize("next_url", ["https://elsewhere.invalid/steal", "https://source.example.invalid/2.0/repositories/OTHER"])
def test_pagination_cannot_escape_pinned_workspace_endpoint(setup, next_url):
    agent, rest = setup;rest.next = next_url
    with pytest.raises(IntegrationNotAuthorized, match="pagination escaped"):
        agent.connections.discover("bb", ADMIN)
    assert len(rest.calls) == 1


def test_jira_discovery_and_project_narrowing_use_same_broker(setup):
    agent, _ = setup
    agent.projects["PCC"]["tracker_connection"] = "jira"
    result = agent.connections.discover("jira", ADMIN)
    agent.connections.select("jira", ADMIN, result["receipt"], ["PCC"])
    tracker = agent.tracker_for("PCC")
    assert tracker.get_issue("PCC-1")["fields"]["summary"] == "Synthetic story"
    with pytest.raises(IntegrationError):
        tracker.get_issue("OTHER-1")


def test_expired_discovery_cannot_change_scope(setup):
    agent, _ = setup
    result = agent.connections.discover("bb", ADMIN)
    agent.store.x("UPDATE connection_discovery SET expires=0")
    with pytest.raises(IntegrationNotAuthorized, match="expired"):
        agent.connections.select("bb", ADMIN, result["receipt"], ["SYN/pim-import-service"])


def test_connection_api_does_not_accept_browser_credentials_and_filters_administrators(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTION_TEST_KEY", "synthetic-secret-only")
    users = UserStore(tmp_path / "users.json")
    admin = users.add("admin", "approver", tenant_id="tenant-a", project_roles={"PCC": "approver"})
    other = users.add("other", "approver", tenant_id="tenant-a", project_roles={"PCC": "approver"})
    cfg = agent_cfg("replay", auth={"mode": "local"})
    cfg["projects"]["PCC"]["tenant_id"] = "tenant-a"
    cfg["connections"] = {"bb": connection()}
    app = create_app(tmp_path / "state", agent_cfg=cfg, users_file=users.path)
    rest = Rest();app.state.agent.connections.transports = {"bb": httpx.MockTransport(rest)}
    try:
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer " + admin}
            result = client.post("/api/connections/bb/discover", headers=headers, json={}).json()
            assert client.post("/api/connections/bb/discover", headers=headers, json={"token": "never-accept-secrets"}).status_code == 422
            bad = client.post("/api/connections/bb/scope", headers=headers, json={"receipt": result["receipt"], "resources": ["SYN/pim-import-service"], "token": "never-accept-secrets"})
            assert bad.status_code == 422 and "never-accept-secrets" not in bad.text
            saved = client.post("/api/connections/bb/scope", headers=headers, json={"receipt": result["receipt"], "resources": ["SYN/pim-import-service"]})
            assert saved.status_code == 200
            assert client.get("/api/connections", headers={"Authorization": "Bearer " + other}).json() == []
            assert client.post("/api/connections/bb/discover", headers={"Authorization": "Bearer " + other}, json={}).status_code == 404
    finally:
        app.state.agent.store.db.close()
