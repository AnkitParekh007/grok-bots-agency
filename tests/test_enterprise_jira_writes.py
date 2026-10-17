"""Offline write contracts. No real Jira, credentials, models or SUT mutations."""
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from qa_agent.auth import UserStore
from qa_agent.server import create_app
from qa_agent.jira_writes import JiraWrites, JiraWriteDenied
from qa_agent.store import Store, FileArtifactStore, SqliteApprovalProvider, verify_chain
from qa_agent.gateway import ToolGateway
from tests.b1_helpers import agent_cfg
from tests.b1_helpers import free_port

pytestmark = [pytest.mark.security, pytest.mark.approval, pytest.mark.audit]


@pytest.fixture
def writes(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_JIRA_WRITE_TEST_TOKEN", "synthetic-write-credential-not-real")
    users = UserStore(tmp_path / "users.json")
    tokens = {name: users.add(name, role) for name, role in
              [("requester", "requester"), ("reviewer", "approver"), ("other", "approver"), ("viewer", "viewer")]}
    cfg = agent_cfg("replay", deployment={"public_url": f"https://localhost:{free_port()}"}, jira_writes={"PCC": {
        "enabled": True, "approved": True, "approval_ref": "synthetic-owner-review", "kind": "jira_cloud",
        "base_url": "https://jira.invalid", "project_key": "PCC", "issue_type_id": "10", "issue_type_name": "Bug",
        "auth": {"type": "bearer", "token_env": "QA_JIRA_WRITE_TEST_TOKEN"},
        "transitions": {"review": {"id": "20", "name": "Review", "from_status_id": "1", "to_status_id": "2"}}}})
    app = create_app(tmp_path / "data", auth_mode="local", users_file=users.path, agent_cfg=cfg)
    agent = app.state.agent
    agent.policy["jira_writes"] = {"approved_hosts": ["jira.invalid"]}
    source_cfg = {"base_url": "https://jira.invalid", "projects": ["PCC"], "enabled": True,
                  "approved": True, "approval_ref": "synthetic-source-review"}
    agent.tracker_for = lambda project: SimpleNamespace(kind="jira_cloud", cfg=source_cfg)
    agent.store.create_run("run", "synthetic", "synthetic-local", "replay", "replay")
    agent.store.update_run("run", status="completed", requested_by="requester", project="PCC", story_key="PCC-1",
                           plan_hash="synthetic-plan", summary_json={"failed": 1},
                           story_json={"key": "PCC-1", "_source": "jira_cloud", "fields": {"summary": "Synthetic issue"}})
    agent.store.save_result("run", {"id": "T1", "status": "failed", "actual": "private-customer-text"})
    agent.artifacts.write_bytes("run", "report.json", b'{"synthetic":true}')
    receipts = {n: {"mode": "local_credential", "principal": vars(users.verify(t)),
                    "revision": app.state.sessions.revision(n)} for n, t in tokens.items()}
    calls = []
    state = {"updated": "2026-10-17T12:00:00.000Z", "status": "1", "post": 201}

    def handler(request):
        calls.append(request)
        assert request.url.host == "jira.invalid"
        if request.method == "POST":
            if state.get("error"):
                raise httpx.ReadTimeout("private-customer-text", request=request)
            status = state["post"]
            if request.url.path.endswith("/transitions") and status == 201:
                return httpx.Response(204)
            return httpx.Response(status, json={"key": "PCC-9", "id": "900"}, headers={"Location": "https://evil.invalid"})
        if "/project/" in request.url.path:
            return httpx.Response(200, json={"key": "PCC", "id": "100", "issueTypes": [{"id": "10", "name": "Bug", "subtask": False}]})
        if request.url.path.endswith("/transitions"):
            return httpx.Response(200, json={"transitions": [{"id": "20", "name": "Review", "to": {"id": "2"}, "fields": {}}]})
        return httpx.Response(200, json={"key": request.url.path.rsplit("/", 1)[-1], "id": "101",
            "fields": {"project": {"key": "PCC"}, "status": {"id": state["status"]}, "updated": state["updated"]}})

    service = app.state.jira_writes
    service.transport = httpx.MockTransport(handler)
    yield SimpleNamespace(app=app, agent=agent, service=service, users=users, tokens=tokens, receipts=receipts,
                          calls=calls, state=state, cfg=cfg)
    agent.close()
    agent.store.db.close()


def proposal(w, kind="create", **kw):
    return w.service.propose("run", kind, w.receipts["requester"], **kw)


def approve(w, draft):
    return w.service.review("run", draft["draft"]["id"], w.receipts["reviewer"], draft["draft_hash"], "approve")


def posts(w):
    return [c for c in w.calls if c.method == "POST"]


@pytest.mark.parametrize("kind,kw", [("create", {}), ("comment", {"target": "PCC-1"}),
                                    ("transition", {"target": "PCC-1", "transition": "review"})])
def test_exact_review_minimal_payload_and_one_dispatch(writes, kind, kw):
    w = writes
    draft = proposal(w, kind, **kw)
    assert draft["state"] == "pending" and not posts(w)
    assert "private-customer-text" not in json.dumps(draft)
    approve(w, draft)
    outcome = w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
    assert outcome["state"] == "succeeded"
    assert json.loads(posts(w)[0].content) == draft["draft"]["payload"]
    assert len(posts(w)) == 1
    with pytest.raises(JiraWriteDenied):
        w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
    assert len(posts(w)) == 1
    assert verify_chain(w.agent.store.audit_rows("run"))["ok"]


def test_duplicate_proposal_returns_original_receipt_without_new_operation(writes):
    one = proposal(writes)
    two = proposal(writes)
    assert one == two
    assert len(writes.agent.store.q("SELECT * FROM jira_writes")) == 1


@pytest.mark.parametrize("mutate", ["disabled", "unapproved", "host", "project", "credentials", "http", "prefix", "kind"])
def test_configuration_fails_before_network(writes, monkeypatch, mutate):
    w = writes
    cfg = w.cfg["jira_writes"]["PCC"]
    if mutate == "disabled": cfg["enabled"] = False
    elif mutate == "unapproved": cfg["approved"] = False
    elif mutate == "host": w.agent.policy["jira_writes"]["approved_hosts"] = []
    elif mutate == "project": cfg["project_key"] = "OTHER"
    elif mutate == "credentials": monkeypatch.delenv("QA_JIRA_WRITE_TEST_TOKEN")
    elif mutate == "http": cfg["base_url"] = "http://jira.invalid"
    elif mutate == "prefix": cfg["base_url"] += "/evil"
    elif mutate == "kind": cfg["kind"] = "mcp"
    with pytest.raises(JiraWriteDenied): proposal(w)
    assert not w.calls


@pytest.mark.parametrize("actor", ["requester", "viewer"])
def test_self_or_unprivileged_review_is_denied(writes, actor):
    w = writes
    draft = proposal(w)
    with pytest.raises(JiraWriteDenied):
        w.service.review("run", draft["draft"]["id"], w.receipts[actor], draft["draft_hash"], "approve")
    assert not posts(w)


@pytest.mark.parametrize("mutate", ["requester_revoked", "reviewer_revoked", "config", "evidence", "artifact", "expiry", "cancelled", "role"])
def test_post_review_drift_never_dispatches(writes, mutate):
    w = writes
    draft = proposal(w)
    approve(w, draft)
    if mutate == "requester_revoked": w.users.disable("requester")
    elif mutate == "reviewer_revoked": w.users.disable("reviewer")
    elif mutate == "config": w.cfg["jira_writes"]["PCC"]["issue_type_id"] = "11"
    elif mutate == "evidence": w.agent.store.save_result("run", {"id": "T1", "status": "passed"})
    elif mutate == "artifact": w.agent.artifacts.write_bytes("run", "report.json", b"changed")
    elif mutate == "expiry": w.service.clock = lambda: draft["draft"]["expires"] + 1
    elif mutate == "cancelled": w.agent.store.update_run("run", cancel_requested=1)
    elif mutate == "role": w.agent.projects["PCC"]["tenant_id"] = "other"
    with pytest.raises(JiraWriteDenied): w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
    assert not posts(w)


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500, 503, 200])
def test_unconfirmed_post_is_uncertain_never_retried(writes, status):
    w = writes
    draft = proposal(w)
    approve(w, draft)
    w.state["post"] = status
    result = w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
    assert result["state"] == "uncertain" and result["outcome"] == {"reconciliation_required": True}
    assert len(posts(w)) == 1
    assert proposal(w)["draft"]["id"] == draft["draft"]["id"]
    with pytest.raises(JiraWriteDenied): w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
    assert len(posts(w)) == 1


def test_timeout_cannot_leak_upstream_text_or_be_replayed_after_reopen(writes):
    w = writes
    draft = proposal(w)
    approve(w, draft)
    w.state["error"] = True
    result = w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
    assert result["state"] == "uncertain" and "private-customer-text" not in json.dumps(result)
    store = Store(w.agent.store.data_dir)
    try:
        other_agent = copy.copy(w.agent)
        other_agent.store = store
        other = JiraWrites(other_agent, w.service.authorization, transport=w.service.transport)
        with pytest.raises(JiraWriteDenied): other.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
        assert len(posts(w)) == 1
    finally:
        store.db.close()


def test_target_and_transition_scope_are_checked_before_write(writes):
    w = writes
    for target in ("OTHER-1", "PCC-2", "PCC-1/../../evil", "https://evil.invalid"):
        with pytest.raises(JiraWriteDenied): proposal(w, "comment", target=target)
    draft = proposal(w, "transition", target="PCC-1", transition="review")
    approve(w, draft)
    w.state["updated"] = "2026-10-17T12:00:01.000Z"
    with pytest.raises(JiraWriteDenied): w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
    assert not posts(w)


def test_distinct_approved_human_must_dispatch_and_hash_is_mandatory(writes):
    w = writes
    draft = proposal(w)
    with pytest.raises(JiraWriteDenied):
        w.service.review("run", draft["draft"]["id"], w.receipts["reviewer"], "0" * 64, "approve")
    approve(w, draft)
    with pytest.raises(JiraWriteDenied): w.service.dispatch("run", draft["draft"]["id"], w.receipts["other"])
    assert not posts(w)


def test_post_intent_revocation_stops_dispatch_and_retains_uncertain_receipt(writes, monkeypatch):
    w = writes
    draft = proposal(w)
    approve(w, draft)
    original = w.agent.gateway.audit_delivery.flush
    def revoke(*args, **kwargs):
        if w.agent.store.q("SELECT state FROM jira_writes")[0]["state"] == "dispatching": w.users.disable("requester")
        return original(*args, **kwargs)
    monkeypatch.setattr(w.agent.gateway.audit_delivery, "flush", revoke)
    result = w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
    assert result["state"] == "uncertain" and not posts(w)


def test_crashed_dispatching_receipt_is_never_reset_by_startup(writes):
    w = writes
    draft = proposal(w)
    approve(w, draft)
    w.agent.store.x("UPDATE jira_writes SET state='dispatching' WHERE id=?", (draft["draft"]["id"],))
    reopened = Store(w.agent.store.data_dir)
    try:
        assert reopened.q("SELECT state FROM jira_writes")[0]["state"] == "dispatching"
        with pytest.raises(JiraWriteDenied): w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
        assert not posts(w)
    finally: reopened.db.close()


def test_api_roles_exact_review_and_cross_owner_visibility(writes):
    w = writes
    headers = lambda name: {"Authorization": "Bearer " + w.tokens[name]}
    client = TestClient(w.app)
    path = "/api/runs/run/jira-writes"
    assert client.post(path, json={"kind": "create"}).status_code == 401
    assert client.post(path, headers=headers("viewer"), json={"kind": "create"}).status_code == 403
    assert client.post(path, headers=headers("requester"), json={"kind": "create", "payload": {}}).status_code == 422
    response = client.post(path, headers=headers("requester"), json={"kind": "create"})
    assert response.status_code == 200, response.text
    draft = response.json()
    operation = path + "/" + draft["draft"]["id"]
    review = {"draft_hash": draft["draft_hash"], "decision": "approve"}
    assert client.post(operation + "/review", headers=headers("requester"), json=review).status_code == 403
    assert client.post(operation + "/review", headers=headers("reviewer"), json=review).status_code == 200
    assert client.post(operation + "/dispatch", headers=headers("other")).status_code == 409
    response = client.post(operation + "/dispatch", headers=headers("reviewer"))
    assert response.status_code == 200 and response.json()["state"] == "succeeded"
    assert len(posts(w)) == 1


def test_independent_database_connections_race_only_one_post(writes):
    w = writes
    draft = proposal(w)
    approve(w, draft)
    other_agent = copy.copy(w.agent)
    store = Store(w.agent.store.data_dir)
    other_agent.store = store
    artifacts = FileArtifactStore(store.data_dir / "runs", store=store)
    other_agent.artifacts = artifacts
    other_agent.gateway = ToolGateway(w.agent.policy, store, SqliteApprovalProvider(store), w.agent.envs,
                                      artifacts, store.data_dir / "audit.jsonl")
    other = JiraWrites(other_agent, w.service.authorization, transport=w.service.transport)
    barrier = threading.Barrier(2)
    original = w.service._upstream
    def synchronized(*args):
        result = original(*args)
        barrier.wait(timeout=15)
        return result
    w.service._upstream = synchronized
    other._upstream = synchronized
    def send(service):
        try:
            return service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])["state"]
        except JiraWriteDenied:
            return "denied"
    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(send, [w.service, other]))
        assert sorted(results) == ["denied", "succeeded"]
        assert len(posts(w)) == 1
    finally:
        store.db.close()


def test_receipts_remain_readable_when_capability_disabled_or_run_cancelled(writes):
    w = writes
    draft = proposal(w)
    w.cfg["jira_writes"]["PCC"]["enabled"] = False
    w.agent.store.update_run("run", status="cancelled", cancel_requested=1)
    inventory = w.service.list("run", w.receipts["reviewer"])
    assert not inventory["available"] and inventory["operations"] == [draft]
    assert not posts(w)


def test_rejected_draft_cannot_be_approved_or_dispatched(writes):
    w = writes
    draft = proposal(w)
    result = w.service.review("run", draft["draft"]["id"], w.receipts["reviewer"], draft["draft_hash"], "reject")
    assert result["state"] == "rejected"
    with pytest.raises(JiraWriteDenied): approve(w, draft)
    with pytest.raises(JiraWriteDenied): w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
    assert not posts(w)


def test_source_status_and_required_transition_fields_fail_closed(writes):
    w = writes
    w.state["status"] = "9"
    with pytest.raises(JiraWriteDenied): proposal(w, "transition", target="PCC-1", transition="review")
    w.state["status"] = "1"
    original = w.service.transport.handler
    def required(request):
        if request.url.path.endswith("/transitions"):
            return httpx.Response(200, json={"transitions": [{"id": "20", "name": "Review", "to": {"id": "2"},
                                                           "fields": {"resolution": {"required": True}}}]})
        return original(request)
    w.service.transport = httpx.MockTransport(required)
    with pytest.raises(JiraWriteDenied): proposal(w, "transition", target="PCC-1", transition="review")
    assert not posts(w)


def test_cross_tenant_or_service_receipts_cannot_propose(writes):
    w = writes
    for update in ({"tenant_id": "other"}, {"kind": "service"}, {"mode": "demo"}):
        receipt = copy.deepcopy(w.receipts["requester"])
        receipt["principal"].update(update)
        with pytest.raises(JiraWriteDenied): w.service.propose("run", "create", receipt)
    assert not w.calls


def test_payload_receipt_and_transport_byte_bounds(writes):
    w = writes
    draft = proposal(w)
    approve(w, draft)
    w.service.transport = httpx.MockTransport(lambda r: httpx.Response(201, content=b"x" * 256001) if
        r.method == "POST" else httpx.Response(200, json={"key": "PCC", "id": "100", "issueTypes": [
            {"id": "10", "name": "Bug", "subtask": False}]}))
    result = w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
    assert result["state"] == "uncertain"


def test_synthetic_or_different_source_cannot_authorize_a_real_write(writes):
    w = writes
    w.agent.tracker_for = lambda project: SimpleNamespace(kind="mock_jira", cfg={})
    with pytest.raises(JiraWriteDenied): proposal(w)
    assert not w.calls


def test_result_bytes_are_bounded_before_loading_or_sending(writes):
    w = writes
    w.agent.store.save_result("run", {"id": "T1", "status": "failed", "actual": "x" * 2_000_001})
    with pytest.raises(JiraWriteDenied, match="count/byte"): proposal(w)
    assert not w.calls


@pytest.mark.parametrize("phase", ["intent", "succeeded"])
def test_audit_failure_before_or_after_post_never_allows_replay(writes, monkeypatch, phase):
    w = writes
    draft = proposal(w)
    approve(w, draft)
    original = w.service._audit
    def fail(run_id, op, current):
        if current == phase: raise RuntimeError("synthetic ledger failure")
        return original(run_id, op, current)
    monkeypatch.setattr(w.service, "_audit", fail)
    with pytest.raises(RuntimeError): w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
    row = w.agent.store.q("SELECT state FROM jira_writes")[0]
    assert row["state"] == ("approved" if phase == "intent" else "dispatching")
    assert len(posts(w)) == (0 if phase == "intent" else 1)
    if phase == "succeeded":
        with pytest.raises(JiraWriteDenied): w.service.dispatch("run", draft["draft"]["id"], w.receipts["reviewer"])
        assert len(posts(w)) == 1


@pytest.mark.integration
def test_browser_human_review_is_separate_from_send_and_keyboard_accessible(writes):
    import asyncio
    import uvicorn
    from urllib.parse import urlsplit
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import hashes, serialization
    from playwright.sync_api import sync_playwright, expect
    from qa_agent import ROOT
    w = writes
    base = w.cfg['deployment']['public_url']
    port = urlsplit(base).port
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(
        x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1)).not_valid_after(
        datetime.now(timezone.utc) + timedelta(hours=1)).add_extension(
        x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False).sign(key, hashes.SHA256())
    cert_path, key_path = w.agent.store.data_dir / 'test-cert.pem', w.agent.store.data_dir / 'test-key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    server = uvicorn.Server(uvicorn.Config(w.app, host="127.0.0.1", port=port, log_level="error",
                                         ssl_certfile=str(cert_path), ssl_keyfile=str(key_path),
                                         timeout_graceful_shutdown=3, timeout_keep_alive=1))
    running = {}
    def run_server():
        # TLS browser preconnects can outlive all requests during shutdown. Keep
        # this test-owned loop cancellable rather than leave a live daemon server.
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(server.serve())
        running.update(loop=loop, task=task)
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            if not server.should_exit: raise
        finally:
            pending = asyncio.all_tasks(loop)
            for item in pending: item.cancel()
            if pending: loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started: break
            time.sleep(.1)
        assert server.started
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            contexts = []
            try:
                for name, viewport in [("requester", {"width": 390, "height": 844}),
                                       ("reviewer", {"width": 1440, "height": 900})]:
                    context = browser.new_context(viewport=viewport, ignore_https_errors=True)
                    contexts.append(context)
                    assert context.request.post(base + "/api/login", data={"token": w.tokens[name]}).ok
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base)
                    if viewport["width"] < 1024: page.locator('[data-action="nav"]').click()
                    page.locator('[data-action="open-run"][data-run="run"]').first.click()
                    if viewport["width"] < 1440: page.locator('#context-toggle').click()
                    page.locator('[data-tab="evidence"]').click()
                    launch = page.get_by_role("button", name="Review Jira writes", exact=True)
                    launch.click()
                    dialog = page.get_by_role("dialog", name="Human-reviewed Jira writes")
                    expect(dialog).to_be_visible()
                    if name == "requester":
                        dialog.get_by_role("button", name="Draft defect", exact=True).click()
                        expect(dialog.locator('pre').first).to_contain_text('QA Buddy investigation: PCC-1')
                        assert dialog.get_by_role("button", name="Approve exact payload").count() == 0
                        assert not posts(w)
                        page.keyboard.press("Escape")
                        expect(dialog).not_to_be_visible()
                        expect(launch).to_be_focused()
                    else:
                        expect(dialog).to_contain_text('https://jira.invalid/rest/api/3/issue')
                        dialog.get_by_role("button", name="Approve exact payload").click()
                        send = dialog.get_by_role("button", name="Send approved Jira write once", exact=True)
                        expect(send).to_be_visible()
                        assert not posts(w)
                        output = ROOT / 'reports/jira-write-review'
                        output.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(output / 'approved-before-dispatch.png'))
                        send.click()
                        expect(dialog).to_contain_text('succeeded')
                        assert len(posts(w)) == 1
                        assert dialog.get_by_role("button", name="Send approved Jira write once").count() == 0
                    assert not errors, errors
            finally:
                for context in contexts: context.close()
                browser.close()
    finally:
        server.should_exit = True
        thread.join(5)
        if thread.is_alive():
            running['loop'].call_soon_threadsafe(running['task'].cancel)
            thread.join(5)
        assert not thread.is_alive()
