"""Synthetic-only profile downgrades, concurrent servers and response fencing."""
import asyncio
import copy
import os
import sqlite3
import time
from dataclasses import asdict

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from starlette.responses import FileResponse

from qa_agent.auth import UserStore, Principal
from qa_agent.gateway import PolicyDenied
from qa_agent.recovery import backup, restore
from qa_agent.server import create_app
from qa_agent.store import Store, SCHEMA_VERSION
from qa_agent.workspace_trust import ProfileChanged, ProfileFence, allowed, level, register, _legacy_minimum
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.security, pytest.mark.approval, pytest.mark.audit]


def close(app):
    app.state.agent.close()
    app.state.agent.connections.close()
    app.state.agent.store.db.close()


@pytest.fixture
def setup(tmp_path):
    users = UserStore(tmp_path / "users.json")
    token = users.add("owner", "requester")
    cfg = agent_cfg("replay")
    apps = []
    def make(mode="demo", directory=None, *, config=None, transport=None):
        app = create_app(directory or tmp_path / "state", auth_mode=mode, users_file=users.path,
                         agent_cfg=copy.deepcopy(config or cfg), oidc_transport=transport)
        apps.append(app)
        return app
    yield make, users, token, tmp_path
    for app in apps:
        close(app)


def private_run(app):
    agent = app.state.agent
    agent.store.create_run("private", "SYNTHETIC-PRIVATE-PROFILE-MARKER", "synthetic-local", "replay", "Replay")
    agent.store.update_run("private", requested_by="owner", project="PCC", status="completed")
    agent.artifacts.write_bytes("private", "report.json", b"SYNTHETIC-PRIVATE-ARTIFACT-MARKER")


def test_authenticated_history_cannot_be_reopened_in_demo(setup):
    make, _, token, _ = setup
    secure = make("local")
    private_run(secure)
    with pytest.raises(ValueError, match="downgrade"): make("demo")
    with TestClient(secure) as client:
        assert client.get("/api/runs").status_code == 401
        response = client.get("/api/runs", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200 and "SYNTHETIC-PRIVATE-PROFILE-MARKER" in response.text


def test_old_demo_server_is_fenced_after_another_server_upgrades_profile(setup):
    make, _, token, _ = setup
    demo = make()
    with TestClient(demo) as old:
        assert old.get("/api/runs").status_code == 200
        secure = make("local")
        private_run(secure)
        for path in ("/", "/api/runs", "/api/runs/private", "/api/runs/private/report.json"):
            response = old.get(path)
            assert response.status_code == 503
            assert "SYNTHETIC-PRIVATE" not in response.text
            assert "no-store" in response.headers["cache-control"].split(", ")
        executed = []
        with pytest.raises(PolicyDenied, match="profile changed"):
            demo.state.agent.gateway.invoke(None, "env.list", {}, lambda: executed.append(True))
        assert executed == []
        authorizer = demo.state.regression.authorization
        assert not authorizer.check({"mode": "operator"}, {}, "planning")
        with TestClient(secure) as current:
            assert current.get("/api/runs", headers={"Authorization": "Bearer " + token}).status_code == 200
            response = current.get("/api/runs/private/report.json", headers={"Authorization": "Bearer " + token})
            assert response.status_code == 200 and response.content == b"SYNTHETIC-PRIVATE-ARTIFACT-MARKER"


def test_trust_is_monotonic_and_shared_history_cannot_use_local_profile(setup):
    make, _, _, _ = setup
    app = make("local")
    store = app.state.agent.store
    register(app.state.agent, 2)
    assert allowed(store, 2) and not allowed(store, 1)
    with pytest.raises(ValueError, match="downgrade"): make("local")
    with pytest.raises(sqlite3.IntegrityError): store.x("UPDATE workspace_trust SET minimum_level=0")
    with pytest.raises(sqlite3.IntegrityError): store.x("DELETE FROM workspace_trust")
    assert level("demo", False) == 0 and level("local", False) == level("oidc", False) == 1
    assert level("oidc", True) == 2
    with pytest.raises(ValueError): level("local", True)


@pytest.mark.parametrize("variant,expected", [("empty", 0), ("run", 1), ("local_review", 1), ("oidc_review", 2), ("oversized_history", 2)])
def test_legacy_state_requires_conservative_authentication(setup, variant, expected):
    make, _, _, root = setup
    store = Store(root / "state")
    if variant in ("run", "local_review", "oidc_review"):
        store.create_run("legacy", "synthetic legacy history", "synthetic-local", "replay", "Replay")
    if variant.endswith("review"):
        store.x("INSERT INTO approvals(run_id,auth_mode,identity) VALUES(?,?,?)", ("legacy", "oidc" if variant == "oidc_review" else "local", "synthetic-reviewer"))
    if variant == "oversized_history":
        store.db.execute("BEGIN IMMEDIATE")
        store.db.executemany("INSERT INTO runs(id) VALUES(?)", [("legacy-" + str(n),) for n in range(1001)])
        store.db.commit()
    assert _legacy_minimum(store) == expected
    store.db.close()
    if expected:
        with pytest.raises(ValueError, match="downgrade"): make()
        inspection = Store(root / "state")
        try:
            assert inspection.q("SELECT minimum_level FROM workspace_trust")[0]["minimum_level"] == expected
        finally:
            inspection.db.close()
    else:
        assert allowed(make().state.agent.store, 0)


def test_legacy_scan_deadline_fails_closed_and_releases_progress_handler(tmp_path, monkeypatch):
    import qa_agent.workspace_trust as trust
    store = Store(tmp_path)
    store.db.execute("BEGIN IMMEDIATE")
    store.db.executemany("INSERT INTO runs(id) VALUES(?)", [("legacy-" + str(n),) for n in range(900)])
    store.db.commit()
    ticks = iter([0] + [2] * 100)
    monkeypatch.setattr(trust.time, "monotonic", lambda: next(ticks, 2))
    try:
        assert _legacy_minimum(store) == 2
        assert store.q("SELECT COUNT(*) AS n FROM runs")[0]["n"] == 900
    finally:
        store.db.close()


def test_native_file_response_cannot_bypass_profile_fence(setup):
    make, _, _, root = setup
    app = make()
    marker = root / "synthetic.bin"
    marker.write_bytes(b"SYNTHETIC-PRIVATE-FILE-MARKER")
    messages = []
    async def inner(scope, receive, send):
        assert "http.response.pathsend" not in scope["extensions"]
        async def upgrade_after_headers(message):
            await send(message)
            if message["type"] == "http.response.start": register(app.state.agent, 1)
        await FileResponse(marker)(scope, receive, upgrade_after_headers)
    async def receive(): return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message): messages.append(message)
    scope = {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}, "extensions": {"http.response.pathsend": {}}}
    with pytest.raises(ProfileChanged): asyncio.run(ProfileFence(inner, store=app.state.agent.store, rank=0)(scope, receive, send))
    assert not any(b"SYNTHETIC-PRIVATE" in m.get("body", b"") or m["type"] == "http.response.pathsend" for m in messages)


def test_profile_change_before_headers_returns_only_unavailable_body(setup):
    make, *_ = setup
    app, messages = make(), []
    async def inner(scope, receive, send):
        register(app.state.agent, 1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"SYNTHETIC-PRIVATE-BODY"})
    async def receive(): return {"type": "http.request", "body": b""}
    async def send(message): messages.append(message)
    asyncio.run(ProfileFence(inner, store=app.state.agent.store, rank=0)({"type": "http", "extensions": {}}, receive, send))
    assert messages[0]["status"] == 503
    assert not any(b"SYNTHETIC-PRIVATE" in m.get("body", b"") for m in messages)


def test_websocket_profile_change_closes_before_sending_private_message(setup):
    make, *_ = setup
    app, messages = make(), []
    async def inner(scope, receive, send):
        await send({"type": "websocket.accept"})
        register(app.state.agent, 1)
        await send({"type": "websocket.send", "text": "SYNTHETIC-PRIVATE-MESSAGE"})
    async def receive(): return {"type": "websocket.connect"}
    async def send(message): messages.append(message)
    asyncio.run(ProfileFence(inner, store=app.state.agent.store, rank=0)({"type": "websocket"}, receive, send))
    assert [m["type"] for m in messages] == ["websocket.accept", "websocket.close"]
    assert messages[-1]["code"] == 1008


def test_encrypted_restore_preserves_minimum_profile(setup):
    make, _, _, root = setup
    app = make("local")
    private_run(app)
    key, archive = os.urandom(32), root / "backup.qab"
    backup(app.state.agent.store.data_dir, archive, key, "synthetic-profile-ledger", confirmed_quiescent=True)
    restore(archive, root / "restored", key, "synthetic-profile-ledger")
    with pytest.raises(ValueError, match="downgrade"): make("demo", root / "restored")
    assert allowed(make("local", root / "restored").state.agent.store, 1)
    assert app.state.agent.store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4


def test_schema_v3_upgrade_preserves_history_and_requires_authentication(setup):
    make, _, _, root = setup
    store = Store(root / "state")
    store.create_run("legacy", "synthetic v3 history preserved", "synthetic-local", "replay", "Replay")
    store.x("DROP TABLE workspace_trust")  # Emulate the previous schema, which had no profile receipt.
    store.x("PRAGMA user_version=3")
    store.db.close()
    with pytest.raises(ValueError, match="downgrade"): make()
    app = make("local")
    assert app.state.agent.store.get_run("legacy")["request"] == "synthetic v3 history preserved"
    assert app.state.agent.store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def oidc_config():
    return agent_cfg("replay", deployment={"profile": "shared", "public_url": "https://workspace.invalid"}, auth={"oidc": {
        "issuer": "https://idp.invalid", "authorization_endpoint": "https://idp.invalid/authorize",
        "token_endpoint": "https://idp.invalid/token", "jwks_uri": "https://idp.invalid/jwks",
        "redirect_uri": "https://workspace.invalid/api/oidc/callback", "client_id": "synthetic-workspace",
        "allowed_hosts": ["idp.invalid"], "approval_ref": "synthetic-policy", "flow_key_env": "QA_PROFILE_TEST_FLOW_KEY",
        "required_amr": ["mfa"]}})


def no_idp_requests():
    def deny(request): pytest.fail("saved-cookie test attempted IdP network")
    return httpx.MockTransport(deny)


def test_local_cookie_cannot_be_replayed_as_shared_oidc_cookie(setup, monkeypatch):
    make, users, _, _ = setup
    monkeypatch.setenv("QA_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("QA_PROFILE_TEST_FLOW_KEY", Fernet.generate_key().decode())
    token = users.add("owner", "requester", project_roles={"PCC": "requester"})
    local = make("local")
    private_run(local)
    saved = local.state.sessions.issue(asdict(users.verify(token)))
    oidc = make("oidc", config=oidc_config(), transport=no_idp_requests())
    with TestClient(oidc, base_url="https://workspace.invalid") as client:
        client.cookies.set("__Host-qa_session", saved["token"])
        response = client.get("/api/runs")
        assert response.status_code == 401 and "SYNTHETIC-PRIVATE" not in response.text
    assert oidc.state.sessions.verify(saved["token"]) is None


def test_http_principal_rejects_wrong_mode_even_with_current_context(setup):
    make, *_ = setup
    app = make("local")
    principal = Principal(name="synthetic-oidc-subject", role="viewer", mode="oidc", tenant_id="local",
                          authorization_revision=app.state.agent.access.revision)
    saved = app.state.sessions.issue(asdict(principal), issuer="https://idp.invalid", subject="synthetic-subject")
    assert app.state.sessions.verify(saved["token"])["mode"] == "oidc"  # Cryptographically valid fixture, wrong HTTP mode.
    with TestClient(app) as client:
        client.cookies.set("qa_session", saved["token"])
        assert client.get("/api/config").status_code == 401


@pytest.mark.parametrize("change", ["unchanged", "issuer", "client", "strength", "authorization", "origin"])
def test_oidc_session_context_tracks_current_trust_policy(setup, monkeypatch, change):
    make, *_ = setup
    monkeypatch.setenv("QA_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("QA_PROFILE_TEST_FLOW_KEY", Fernet.generate_key().decode())
    cfg = oidc_config()
    first = make("oidc", config=cfg, transport=no_idp_requests())
    principal = Principal(name="synthetic-oidc-subject", role="viewer", mode="oidc", tenant_id="local",
                          authorization_revision=first.state.agent.access.revision)
    with TestClient(first, base_url="https://workspace.invalid"):
        saved = first.state.sessions.issue(asdict(principal), issuer="https://idp.invalid", subject="synthetic-subject")
    changed = copy.deepcopy(cfg)
    oidc = changed["auth"]["oidc"]
    if change == "issuer":
        oidc.update(issuer="https://other-idp.invalid", authorization_endpoint="https://other-idp.invalid/authorize",
                    token_endpoint="https://other-idp.invalid/token", jwks_uri="https://other-idp.invalid/jwks", allowed_hosts=["other-idp.invalid"])
    elif change == "client": oidc["client_id"] = "different-synthetic-client"
    elif change == "strength": oidc["required_amr"] = ["mfa", "synthetic-extra-strength"]
    elif change == "authorization": changed["authorization"] = {"mode": "project", "group_grants": {}}
    elif change == "origin":
        changed["deployment"]["public_url"] = "https://workspace2.invalid"
        oidc["redirect_uri"] = "https://workspace2.invalid/api/oidc/callback"
    current = make("oidc", config=changed, transport=no_idp_requests())
    with TestClient(current, base_url=changed["deployment"]["public_url"]) as client:
        client.cookies.set("__Host-qa_session", saved["token"])
        response = client.get("/api/config")
        assert response.status_code == (200 if change == "unchanged" else 401)
