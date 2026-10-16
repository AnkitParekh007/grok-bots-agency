"""Server boundaries exercised through real ASGI requests and a signed mock IdP."""
import copy
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from qa_agent.auth import Principal, UserStore
from qa_agent.authorization import AccessPolicy
from qa_agent.server import create_app
from tests.b1_helpers import agent_cfg, wait_status

pytestmark = [pytest.mark.security, pytest.mark.approval]


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def shared(tmp_path, monkeypatch, key):
    monkeypatch.setenv("TEST_SESSION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TEST_FLOW_KEY", Fernet.generate_key().decode())
    cfg = agent_cfg("replay")
    pc = cfg["projects"]["PCC"]
    pc["tenant_id"] = "tenant-a"
    cfg["projects"]["OTHER"] = {**copy.deepcopy(pc), "tenant_id": "tenant-b"}
    cfg["deployment"] = {"profile": "shared", "public_url": "https://workspace.invalid"}
    cfg["auth"].update(mode="oidc", sessions={"key_env": "TEST_SESSION_KEY"}, oidc={
        "issuer": "https://issuer.invalid", "authorization_endpoint": "https://issuer.invalid/authorize",
        "token_endpoint": "https://issuer.invalid/token", "jwks_uri": "https://issuer.invalid/jwks",
        "redirect_uri": "https://workspace.invalid/api/oidc/callback", "client_id": "qa-workspace",
        "allowed_hosts": ["issuer.invalid"], "approval_ref": "synthetic-idp", "flow_key_env": "TEST_FLOW_KEY",
        "required_amr": ["mfa"]})
    cfg["authorization"] = {"mode": "project", "group_grants": {
        "request-a": {"tenant_id": "tenant-a", "projects": {"PCC": "requester"}},
        "review-a": {"tenant_id": "tenant-a", "projects": {"PCC": "approver"}},
        "review-b": {"tenant_id": "tenant-b", "projects": {"OTHER": "approver"}}}}
    issued, calls = {}, []
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="mock-key", alg="RS256", use="sig")
    def respond(req):
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json={"keys": [jwk]} if req.url.path == "/jwks" else {"id_token": issued["token"]})
    app = create_app(tmp_path / "state", agent_cfg=cfg, oidc_transport=httpx.MockTransport(respond))
    def login(client, subject="requester", group="request-a", **over):
        first = client.get("/api/oidc/start", follow_redirects=False)
        assert first.status_code == 303
        params = parse_qs(urlsplit(first.headers["location"]).query)
        claims = {"iss": "https://issuer.invalid", "aud": "qa-workspace", "sub": subject, "name": subject,
                  "iat": int(time.time()), "auth_time": int(time.time()), "exp": int(time.time()) + 600, "nonce": params["nonce"][0],
                  "sid": subject + "-sid", "amr": ["mfa"], "groups": [group], **over}
        issued["token"] = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "mock-key"})
        response = client.get("/api/oidc/callback", params={"state": params["state"][0], "code": "synthetic-code"}, follow_redirects=False)
        return response
    yield app, cfg, login, issued, calls
    app.state.agent.store.db.close()


def client_for(app):
    return TestClient(app, base_url="https://workspace.invalid")


def csrf(client):
    return {"Origin": "https://workspace.invalid", "X-CSRF-Token": client.cookies.get("__Host-qa_csrf")}


def test_shared_profile_refuses_demo_even_when_overridden(tmp_path):
    cfg = agent_cfg("replay", deployment={"profile": "shared", "public_url": "https://workspace.invalid"})
    with pytest.raises(ValueError, match="OIDC"):
        create_app(tmp_path, agent_cfg=cfg, auth_mode="demo")


def test_oidc_session_is_secure_scoped_and_revoked_on_logout(shared):
    app, _, login, _, _ = shared
    with client_for(app) as c:
        assert c.get("/api/projects").status_code == 401
        response = login(c)
        assert response.status_code == 303
        cookie = response.headers.get_list("set-cookie")
        assert any("__Host-qa_session=" in h and "HttpOnly" in h and "Secure" in h and "SameSite=strict" in h for h in cookie)
        assert c.get("/api/me").json()["tenant_id"] == "tenant-a"
        assert [p["key"] for p in c.get("/api/projects").json()] == ["PCC"]
        assert c.get("/api/projects/PCC/stories/PCC-DEMO-1").status_code == 200
        assert c.get("/api/projects/OTHER/stories/PCC-DEMO-1").status_code == 404
        old_session = c.cookies.get("__Host-qa_session")
        assert c.post("/api/logout", headers=csrf(c)).status_code == 200
        c.cookies.set("__Host-qa_session", old_session, domain="workspace.invalid", path="/")
        assert c.get("/api/projects").status_code == 401


def test_cookie_mutations_need_csrf_and_exact_origin(shared):
    app, _, login, _, _ = shared
    with client_for(app) as c:
        login(c)
        body = {"message": "Plan PCC-DEMO-1", "project": "PCC", "story_key": "PCC-DEMO-1"}
        assert c.post("/api/chat", json=body).status_code == 403
        assert c.post("/api/chat", json=body, headers={**csrf(c), "Origin": "https://evil.workspace.invalid"}).status_code == 403
        assert c.post("/api/chat", json=body, headers=csrf(c)).status_code == 200
        app.state.agent.threads[-1].join(30)
        assert c.post("/api/login", json={"token": "any-local-token"}, headers=csrf(c)).status_code == 403


def test_foreign_approver_cannot_read_or_cancel_or_approve_run(shared):
    app, _, login, _, _ = shared
    a = app.state.agent
    with client_for(app) as owner, client_for(app) as foreign:
        login(owner);login(foreign, "foreign-reviewer", "review-b")
        r = owner.post("/api/chat", json={"message": "Plan PCC-DEMO-1", "project": "PCC", "story_key": "PCC-DEMO-1"}, headers=csrf(owner))
        assert r.status_code == 200
        rid = r.json()["run_id"];a.threads[-1].join(30)
        assert owner.get(f"/api/runs/{rid}/artifacts/inputs/config.json").status_code == 404
        for path in (f"/api/runs/{rid}", f"/api/runs/{rid}/usage", f"/api/runs/{rid}/artifacts/inputs/config.json"):
            assert foreign.get(path).status_code == 404
        assert foreign.get("/api/runs").json() == []
        assert foreign.post(f"/api/runs/{rid}/cancel", headers=csrf(foreign)).status_code == 404
        assert foreign.post(f"/api/runs/{rid}/approval", json={"decision": "approved", "plan_hash": a.store.get_run(rid)["plan_hash"]}, headers=csrf(foreign)).status_code == 404
        assert a.approvals.latest(rid) is None


def test_oidc_callback_strength_and_subject_validation(shared):
    app, _, login, _, _ = shared
    with client_for(app) as c:
        assert login(c, amr=["pwd"]).status_code == 401
        assert not c.get("/api/me").json()["authenticated"]


def test_changed_group_policy_invalidates_cached_session(shared):
    app, cfg, login, _, _ = shared
    with client_for(app) as c:
        login(c)
        assert c.get("/api/projects").status_code == 200
        cfg["authorization"]["group_grants"].pop("request-a")
        assert c.get("/api/projects").status_code == 401


def test_validated_backchannel_event_revokes_only_its_session(shared, key):
    app, _, login, _, _ = shared
    with client_for(app) as c:
        login(c)
        payload = {"iss": "https://issuer.invalid", "aud": "qa-workspace", "iat": int(time.time()), "jti": "logout-one",
                   "sid": "requester-sid", "events": {"http://schemas.openid.net/event/backchannel-logout": {}}}
        token = jwt.encode(payload, key, algorithm="RS256", headers={"kid": "mock-key"})
        assert c.post("/api/oidc/backchannel", data={"logout_token": token}).status_code == 200
        assert c.get("/api/projects").status_code == 401
        assert c.post("/api/oidc/backchannel", data={"logout_token": token}).status_code == 400


def test_scoped_local_service_cannot_be_a_human_approver(tmp_path):
    users = UserStore(tmp_path / "users.json")
    token = users.add("service", "approver", project_roles={"PCC": "approver"}, kind="service",
                      expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    cfg = agent_cfg("replay", authorization={"mode": "project"})
    app = create_app(tmp_path / "state", agent_cfg=cfg, auth_mode="local", users_file=users.path)
    a = app.state.agent
    rid = a.start_run("Plan PCC-DEMO-1", story_key="PCC-DEMO-1", project="PCC", requested_by="someone-else", wait=True)
    with TestClient(app) as c:
        r = c.post(f"/api/runs/{rid}/approval", json={"decision": "approved", "plan_hash": a.store.get_run(rid)["plan_hash"]},
                   headers={"Authorization": "Bearer " + token})
        assert r.status_code == 403 and "human" in r.text
    a.store.db.close()


def test_global_role_does_not_escalate_weaker_project_grant():
    policy = AccessPolicy({"PCC": {"tenant_id": "tenant-a"}}, {"mode": "project"})
    p = Principal("person", "approver", "local", "tenant-a", {"PCC": "viewer"})
    assert policy.allows(p, "PCC", "viewer")
    assert not policy.allows(p, "PCC", "requester")


def test_oversized_body_and_validation_do_not_echo_token_material(tmp_path):
    app = create_app(tmp_path, agent_cfg=agent_cfg("replay"), auth_mode="local")
    with TestClient(app) as c:
        r = c.post("/api/login", json={"token": "synthetic-private-marker" * 100})
        assert r.status_code == 422 and "synthetic-private-marker" not in r.text
        r = c.post("/api/chat", content=b"x" * 1_000_001, headers={"Content-Type": "application/json"})
        assert r.status_code == 413
        assert c.get("/api/me", headers={"Host": "attacker.invalid"}).status_code == 400
    app.state.agent.store.db.close()


@pytest.mark.integration
@pytest.mark.execution
def test_distinct_oidc_human_approves_real_synthetic_execution(shared, monkeypatch, sut_factory):
    app, _, login, _, _ = shared
    with sut_factory("") as sut:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", sut)
        with client_for(app) as owner, client_for(app) as reviewer:
            login(owner)
            login(reviewer, "reviewer", "review-a")
            r = owner.post("/api/chat", json={"message": "Plan PCC-DEMO-1", "project": "PCC", "story_key": "PCC-DEMO-1"}, headers=csrf(owner))
            assert r.status_code == 200
            rid = r.json()["run_id"]
            planned = wait_status(lambda: owner.get(f"/api/runs/{rid}").json(), {"awaiting_approval", "failed"}, timeout=60)
            assert planned["status"] == "awaiting_approval", planned.get("error")
            approved = reviewer.post(f"/api/runs/{rid}/approval", json={"decision": "approved", "plan_hash": planned["plan_hash"]}, headers=csrf(reviewer))
            assert approved.status_code == 200 and approved.json()["authenticated"]
            finished = wait_status(lambda: owner.get(f"/api/runs/{rid}").json(), {"completed", "failed", "blocked"}, timeout=240)
            assert finished["status"] == "completed", finished.get("error")
            assert finished["summary_json"] == {"total": 8, "passed": 8, "failed": 0, "error": 0, "blocked": 0}
            assert finished["approval"]["auth_mode"] == "oidc"
            assert finished["approval"]["identity"] != finished["requested_by"]
            report = owner.get(f"/api/runs/{rid}/report.json")
            assert report.status_code == 200
            assert report.json()["approval"]["plan_hash"] == planned["plan_hash"]
