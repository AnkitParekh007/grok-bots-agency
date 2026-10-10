import json, sqlite3, subprocess, sys
import pytest
from fastapi.testclient import TestClient
from qa_agent import ROOT
from qa_agent.auth import UserStore
from qa_agent.audit_verify import verify_data_dir
from qa_agent.runtime import QAAgent
from qa_agent.server import create_app
from qa_agent.util import canon, sha256
from tests.b1_helpers import wait_status

PROMPT = "Read the synthetic supplier import story, generate test cases, execute the approved tests, and give me the results."


@pytest.fixture
def local(tmp_path, monkeypatch, sut_factory):
    with sut_factory("all") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        us = UserStore(tmp_path / "users.json")
        toks = {n: us.add(n, r) for n, r in [("vic", "viewer"), ("rita", "requester"), ("bob", "approver"), ("carol", "approver")]}
        app = create_app(tmp_path / "data", auth_mode="local", users_file=tmp_path / "users.json")
        yield TestClient(app), toks, app.state.agent


def H(t):
    return {"Authorization": f"Bearer {t}"}


def new_run(c, tok):
    rid = c.post("/api/chat", json={"message": PROMPT}, headers=H(tok)).json()["run_id"]
    return rid, wait_status(lambda: c.get(f"/api/runs/{rid}", headers=H(tok)).json(), {"awaiting_approval", "failed"})


def test_users_file_stores_only_hashes(tmp_path):
    us = UserStore(tmp_path / "u.json")
    tok = us.add("ann", "viewer")
    raw = (tmp_path / "u.json").read_text()
    assert tok.split(".", 1)[1] not in raw and us.verify(tok).name == "ann"
    assert us.verify(tok + "x") is None and us.verify("ann.wrong") is None and us.verify(None) is None


def test_role_enforcement_and_approval_identity(local):
    c, t, agent = local
    assert c.get("/api/runs").status_code == 401
    assert c.get("/api/runs", headers=H("bob.not-a-token")).status_code == 401
    assert c.get("/api/runs", headers=H(t["vic"])).status_code == 200
    assert c.post("/api/chat", json={"message": PROMPT}, headers=H(t["vic"])).status_code == 403
    assert any(r["tool"] == "http.authz" and r["decision"] == "deny" for r in agent.store.q("SELECT * FROM audit"))
    rid, run = new_run(c, t["rita"])
    ph = run["plan_hash"]
    url = f"/api/runs/{rid}/approval"
    assert c.post(url, json={"decision": "approved", "plan_hash": ph}, headers=H(t["rita"])).status_code == 403   # requester
    assert c.post(url, json={"decision": "approved"}, headers=H(t["bob"])).status_code == 400                    # no plan hash
    r = c.post(url, json={"decision": "approved", "plan_hash": "0" * 64}, headers=H(t["bob"]))
    assert r.status_code == 409 and "stale" in r.text                                                         # stale plan
    assert any(x["tool"] == "plan.approve" and "stale" in x["reason"] for x in agent.store.audit_rows(rid))
    r = c.post(url, json={"decision": "approved", "plan_hash": ph, "approver": "Bob the label"}, headers=H(t["bob"]))
    assert r.status_code == 200 and r.json()["authenticated"] is True
    assert c.post(url, json={"decision": "approved", "plan_hash": ph}, headers=H(t["carol"])).status_code == 409  # duplicate
    done = wait_status(lambda: c.get(f"/api/runs/{rid}", headers=H(t["vic"])).json(), {"completed"})
    a = done["approval"]
    assert (a["identity"], a["approver"], a["auth_mode"], a["approver_label"]) == ("bob", "bob", "local", "Bob the label")
    rep = json.loads(c.get(f"/api/runs/{rid}/report.json", headers=H(t["vic"])).content)
    assert rep["approval_identity_note"] == "authenticated local identity"


def test_self_approval_forbidden(local):
    c, t, _ = local
    rid, run = new_run(c, t["carol"])
    r = c.post(f"/api/runs/{rid}/approval", json={"decision": "approved", "plan_hash": run["plan_hash"]}, headers=H(t["carol"]))
    assert r.status_code == 403 and "own run" in r.text


def test_cookie_session_login(local):
    c, t, _ = local
    assert c.post("/api/login", json={"token": "vic.bad"}).status_code == 401
    assert c.post("/api/login", json={"token": t["vic"]}).json()["user"] == "vic"
    assert c.get("/api/runs").status_code == 200          # cookie
    assert c.get("/api/me").json() == {"user": "vic", "role": "viewer", "mode": "local", "authenticated": True}


def test_rejection_enforced(local):
    c, t, agent = local
    rid, run = new_run(c, t["rita"])
    assert c.post(f"/api/runs/{rid}/approval", json={"decision": "rejected", "plan_hash": run["plan_hash"]}, headers=H(t["bob"])).status_code == 200
    agent.execute(rid, wait=True)
    r = agent.store.get_run(rid)
    assert r["status"] == "blocked" and agent.store.results(rid) == []
    assert not any(x["tool"].startswith(("http.request", "browser.")) for x in agent.store.audit_rows(rid))


def test_demo_mode_marks_approver_unauthenticated(tmp_path, monkeypatch, sut_factory):
    with sut_factory("") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        c = TestClient(create_app(tmp_path, auth_mode="demo"))
        rid = c.post("/api/chat", json={"message": PROMPT}).json()["run_id"]
        wait_status(lambda: c.get(f"/api/runs/{rid}").json(), {"awaiting_approval"})
        assert c.post(f"/api/runs/{rid}/approval", json={"decision": "approved", "approver": "anyone"}).json()["authenticated"] is False
        r = wait_status(lambda: c.get(f"/api/runs/{rid}").json(), {"completed"})
        assert r["approval"]["identity"] is None and r["approval"]["auth_mode"] == "demo"
        assert "UNAUTHENTICATED" in c.get(f"/api/runs/{rid}/report.html").text


def test_plan_tampered_after_approval_blocks_execution(tmp_path):
    a = QAAgent(tmp_path)
    rid = a.start_run(PROMPT, wait=True)
    run = a.store.get_run(rid)
    a.approvals.record(rid, run["plan_hash"], "x", "approved")
    p = run["plan_json"]; p["test_cases"][0]["params"]["file"] = "supplier_missing_sku.csv"
    a.store.update_run(rid, plan_json=p)                      # hash column unchanged, content changed
    a.execute(rid, wait=True)
    assert a.store.get_run(rid)["status"] == "blocked" and a.store.results(rid) == []
    assert any(r["tool"] == "plan.execute" and r["decision"] == "deny" for r in a.store.audit_rows(rid))


def test_audit_chain_verify_and_tamper_detection(tmp_path):
    a = QAAgent(tmp_path)
    a.start_run(PROMPT, wait=True)
    from qa_agent.gateway import PolicyDenied
    with pytest.raises(PolicyDenied):
        a.gateway.invoke("rx", "shell.exec", {"cmd": "id"}, lambda: None)
    v = a.store.verify_audit_chain()
    assert v["ok"] and v["checked"] >= 4
    rows = a.store.q("SELECT * FROM audit ORDER BY id")
    assert rows[0]["prev_hash"] == "GENESIS" and all(rows[i]["prev_hash"] == rows[i - 1]["entry_hash"] for i in range(1, len(rows)))
    assert rows[-1]["decision"] == "deny"                         # denials are in the chain too
    cli = subprocess.run([sys.executable, "-m", "qa_agent.audit_verify", "--data-dir", str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
    assert cli.returncode == 0 and json.loads(cli.stdout)["ok"]
    # tamper SQLite: an attacker with file access drops the trigger and rewrites a denial into an allow
    db = sqlite3.connect(tmp_path / "qa_agent.db")
    db.execute("DROP TRIGGER audit_no_update")
    db.execute("UPDATE audit SET decision='allow' WHERE id=?", (rows[-1]["id"],)); db.commit()
    v = verify_data_dir(tmp_path)
    assert not v["ok"] and v["sqlite"]["broken_at_id"] == rows[-1]["id"] and "modified" in v["sqlite"]["reason"]
    # tamper: delete a row in the middle
    db.execute("DROP TRIGGER audit_no_delete"); db.execute("DELETE FROM audit WHERE id=2"); db.commit()
    assert "prev_hash mismatch" in verify_data_dir(tmp_path)["sqlite"]["reason"]
    # tamper JSONL
    jl = tmp_path / "audit.jsonl"
    lines = jl.read_text().splitlines()
    rec = json.loads(lines[1]); rec["tool"] = "something.else"; lines[1] = json.dumps(rec)
    jl.write_text("\n".join(lines) + "\n")
    cli = subprocess.run([sys.executable, "-m", "qa_agent.audit_verify", "--data-dir", str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
    assert cli.returncode == 1 and json.loads(cli.stdout)["jsonl"]["ok"] is False


def test_audit_verify_endpoint(tmp_path):
    c = TestClient(create_app(tmp_path))
    c.post("/api/chat", json={"message": PROMPT})
    import time; time.sleep(1)
    assert c.get("/api/audit/verify").json()["ok"] is True
