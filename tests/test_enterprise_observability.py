"""Readiness, bounded metric labels, operator auth and log minimization."""
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from qa_agent.auth import UserStore
from qa_agent.server import create_app
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.security, pytest.mark.audit]


@pytest.fixture
def app(tmp_path):
    users = UserStore(tmp_path / "users.json")
    tokens = {role: users.add(role, role) for role in ("viewer", "approver", "requester")}
    app = create_app(tmp_path / "state", agent_cfg=agent_cfg("replay", auth={"mode": "local"}), users_file=users.path)
    yield app, tokens
    app.state.agent.store.db.close()


def headers(token):
    return {"Authorization": "Bearer " + token}


def test_liveness_is_minimal_and_private_health_requires_operator(app):
    server, tokens = app
    with TestClient(server) as client:
        assert client.get("/healthz").json() == {"alive": True}
        ready = client.get("/readyz")
        assert ready.status_code == 200 and ready.json() == {"ready": True, "scope": "process"}
        assert client.get("/api/health").status_code == 401
        assert client.get("/api/health", headers=headers(tokens["viewer"])).status_code == 403
        health = client.get("/api/health", headers=headers(tokens["approver"])).json()
        assert health["ready"] is True and health["enterprise_qualified"] is False


def test_dedicated_scrape_token_has_no_application_role_and_rotates(app, monkeypatch):
    server, _ = app
    first, second = "synthetic-metrics-first-" + "x" * 32, "synthetic-metrics-second-" + "x" * 32
    monkeypatch.setenv("QA_METRICS_TOKEN", first)
    with TestClient(server) as client:
        assert client.get("/metrics", headers=headers(first)).status_code == 200
        assert client.get("/api/projects", headers=headers(first)).status_code == 401
        monkeypatch.setenv("QA_METRICS_TOKEN", second)
        assert client.get("/metrics", headers=headers(first)).status_code == 401
        assert client.get("/metrics", headers=headers(second)).status_code == 200


def test_raw_ids_query_strings_bodies_and_credentials_are_absent_from_access_log_and_labels(app, caplog):
    server, tokens = app
    with caplog.at_level("INFO", logger="qa_agent.access"), TestClient(server) as client:
        response = client.get("/api/runs/private-source-run?token=query-secret", headers=headers(tokens["viewer"]))
        assert response.status_code == 404 and len(response.headers["x-request-id"]) == 32
        client.post("/api/login", json={"token": "body-secret"})
        metrics = client.get("/metrics", headers=headers(tokens["approver"])).text
    events = [json.loads(record.message) for record in caplog.records if record.name == "qa_agent.access"]
    text = json.dumps(events) + metrics
    assert all(value not in text for value in ("private-source-run", "query-secret", "body-secret", tokens["viewer"]))
    assert 'route="/api/runs/{run_id}"' in metrics
    assert all(set(event) == {"event", "request_id", "route", "method", "status", "duration_ms"} for event in events)


def test_audit_backlog_degrades_readiness_and_refuses_new_planning(app):
    server, tokens = app
    agent = server.state.agent
    with TestClient(server) as client, patch("qa_agent.audit_delivery._append", side_effect=OSError("synthetic disk failure")):
        agent.gateway.record(None, "synthetic", {}, "allow", "test")
        agent.policy["audit_delivery"] = {"max_pending": 1}
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        denied = client.post("/api/chat", headers=headers(tokens["requester"]), json={"message": "PCC-DEMO-1"})
        assert denied.status_code == 503 and agent.store.list_runs() == []


def test_security_rejections_still_have_request_id_and_security_headers(app):
    server, _ = app
    with TestClient(server) as client:
        response = client.post("/api/login", headers={"Origin": "https://elsewhere.invalid"}, json={"token": "secret"})
        assert response.status_code == 403
        assert len(response.headers["x-request-id"]) == 32
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
