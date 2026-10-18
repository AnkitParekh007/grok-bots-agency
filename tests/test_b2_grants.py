"""PR #2 third review: server-side eval grants, resumable first-plan review, eval-only profile. No live model calls."""
import copy, importlib.util, json, sqlite3, subprocess, sys
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from qa_agent import ROOT
from qa_agent.auth import UserStore, Principal
from qa_agent.eval_authorization import create_grant, revoke_grant, load_grant, AuthorizationError
from qa_agent.runtime import QAAgent, ConflictError
from qa_agent.server import create_app
from qa_agent.util import load_json
from tests.b1_helpers import wait_status

pytestmark = [pytest.mark.approval, pytest.mark.security]
ACTIONS = ["api_import", "ui_import_error", "ui_import_and_count", "api_product_after_import"]
SKILLS = ["pim_import_testing", "attribute_mapping_validation"]


def future(h=2):
    return (datetime.now(timezone.utc) + timedelta(hours=h)).isoformat()


def spec(**over):
    return {"stories": ["PCC-DEMO-5", "PCC-DEMO-6"], "environment": "synthetic-local", "skills": SKILLS, "actions": ACTIONS,
            "model_classes": ["REPLAYED"], "max_uses": 5, "expires_at": future(), "require_first_manual_approval": False, **over}


@pytest.fixture
def env(tmp_path):
    us = UserStore(tmp_path / "u.json")
    toks = {n: us.add(n, r) for n, r in [("bob", "approver"), ("carol", "approver"), ("rita", "requester"), ("vic", "viewer")]}
    P = {n: us.verify(t) for n, t in toks.items()}
    cfg = load_json(ROOT / "config/agent.json"); cfg["model"]["provider"] = "replay"; cfg["unattended_eval_approval"]["enabled"] = True
    a = QAAgent(tmp_path / "data", agent_cfg=cfg)
    return a, P, toks, cfg, tmp_path


def planned(a, key="PCC-DEMO-5"):
    rid = a.start_run(f"Plan {key}", story_key=key, wait=True)
    assert a.store.get_run(rid)["status"] == "awaiting_approval"
    return rid


def refused(a, rid, gid, match):
    with pytest.raises(AuthorizationError, match=match):
        a.approve_unattended(rid, gid)
    assert a.store.get_run(rid)["status"] == "awaiting_approval" and a.approvals.latest(rid) is None
    assert any(r["tool"] == "plan.approve_unattended" and r["decision"] == "deny" for r in a.store.audit_rows(rid))


# ---------------- Task 2: server-side grants ----------------
def test_grant_happy_path_identity_from_session(env):
    a, P, *_ = env
    g = create_grant(a.store, a.gateway, P["bob"], spec(created_by="mallory", approver_identity="mallory"))   # ignored
    assert g["created_by"] == "bob" and g["grant_id"].startswith("egr-")
    rid = planned(a)
    a.approve_unattended(rid, g["grant_id"], wait=True)
    ap = a.approvals.latest(rid)
    assert (ap["identity"], ap["auth_mode"], ap["approver_label"]) == ("bob", "unattended-synthetic-eval", g["grant_id"])
    assert any(r["tool"] == "eval.grant_create" and r["decision"] == "allow" for r in a.store.q("SELECT * FROM audit"))


@pytest.mark.parametrize("who,match", [("rita", "only approvers"), ("vic", "only approvers")])
def test_non_approver_cannot_create(env, who, match):
    a, P, *_ = env
    with pytest.raises(AuthorizationError, match=match):
        create_grant(a.store, a.gateway, P[who], spec())
    with pytest.raises(AuthorizationError, match="authenticated"):
        create_grant(a.store, a.gateway, Principal(name=None, role="approver", mode="demo"), spec())
    assert a.store.q("SELECT COUNT(*) n FROM eval_grants")[0]["n"] == 0


@pytest.mark.parametrize("over,match", [
    ({"stories": ["PCC-DEMO-2"]}, "story PCC-DEMO-5 not in grant scope"),
    ({"environment": "other-env"}, "environment"),
    ({"actions": ["api_import"]}, "outside the grant scope"),
    ({"skills": ["pim_import_testing"]}, "outside the grant scope"),
    ({"model_classes": ["LIVE MODEL"]}, "model class")])
def test_wrong_scope(env, over, match):
    a, P, *_ = env
    g = create_grant(a.store, a.gateway, P["bob"], spec(**over))
    refused(a, planned(a), g["grant_id"], match)


@pytest.mark.parametrize("source", ["real-adapter", "classified-bundle", "missing-synthetic-label"])
def test_organization_sources_cannot_use_legacy_synthetic_eval_grants(env, source):
    a, P, *_ = env
    g = create_grant(a.store, a.gateway, P["bob"], spec())
    rid = planned(a)
    run = a.store.get_run(rid)
    if source == "classified-bundle":
        a.store.update_run(rid, source_bundle_json={"classification": "internal"})
    else:
        story = copy.deepcopy(run["story_json"])
        if source == "real-adapter": story["_source"] = "jira_cloud"
        else: story["fields"]["labels"] = []
        a.store.update_run(rid, story_json=story)
    refused(a, rid, g["grant_id"], "synthetic fixture sources")
    assert a.store.get_grant(g["grant_id"])["uses"] == 0


def test_expired(env):
    a, P, *_ = env
    with pytest.raises(AuthorizationError, match="future"):
        create_grant(a.store, a.gateway, P["bob"], spec(expires_at="2020-01-01T00:00:00+00:00"))
    g = create_grant(a.store, a.gateway, P["bob"], spec())
    # simulate time passing: move "now" past expiry
    import qa_agent.eval_authorization as EA
    real = EA.datetime
    class Later(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + timedelta(hours=3)
    EA.datetime = Later
    try:
        refused(a, planned(a), g["grant_id"], "expired")
    finally:
        EA.datetime = real


def test_exhausted_and_atomic_counting(env):
    a, P, *_ = env
    g = create_grant(a.store, a.gateway, P["bob"], spec(max_uses=1))
    a.approve_unattended(planned(a), g["grant_id"], wait=True)
    refused(a, planned(a), g["grant_id"], "exhausted or revoked")
    assert a.store.get_grant(g["grant_id"])["uses"] == 1


def test_revoked(env):
    a, P, *_ = env
    g = create_grant(a.store, a.gateway, P["bob"], spec())
    with pytest.raises(AuthorizationError):
        revoke_grant(a.store, a.gateway, P["rita"], g["grant_id"])
    revoke_grant(a.store, a.gateway, P["carol"], g["grant_id"])
    refused(a, planned(a), g["grant_id"], "revoked")
    assert any(r["tool"] == "eval.grant_revoke" and r["decision"] == "allow" for r in a.store.q("SELECT * FROM audit"))


def test_edited_grant_row_rejected(env):
    a, P, *_ = env
    g = create_grant(a.store, a.gateway, P["bob"], spec(stories=["PCC-DEMO-6"]))
    a.store.x("UPDATE eval_grants SET stories=? WHERE grant_id=?", (json.dumps(["PCC-DEMO-5", "PCC-DEMO-6"]), g["grant_id"]))
    refused(a, planned(a), g["grant_id"], "integrity check failed")
    a.store.x("UPDATE eval_grants SET max_uses=999 WHERE grant_id=?", (g["grant_id"],))
    refused(a, planned(a), g["grant_id"], "integrity check failed")


def test_forged_grant_row_rejected(env):
    a, P, *_ = env
    g = create_grant(a.store, a.gateway, P["bob"], spec())
    forged = {**a.store.get_grant(g["grant_id"]), "grant_id": "egr-forged0000000000"}
    from qa_agent.eval_authorization import _grant_hash
    forged["grant_hash"] = _grant_hash(forged)              # internally consistent row, but never created via the API
    a.store.insert_grant(forged)
    refused(a, planned(a), "egr-forged0000000000", "no audited creation")


def test_self_declared_identity_file_path_removed():
    assert not (ROOT / "fixtures/eval/authorizations").exists() or not list((ROOT / "fixtures/eval/authorizations").glob("*.json"))
    import inspect, qa_agent.eval_authorization as EA
    assert not hasattr(EA, "load_authorization")
    assert "authorization_path" not in inspect.signature(QAAgent.approve_unattended).parameters


def test_grant_http_api_local_mode(env):
    a, P, toks, cfg, tmp = env
    c = TestClient(create_app(tmp / "data2", auth_mode="local", users_file=tmp / "u.json", agent_cfg=cfg))
    H = lambda n: {"Authorization": f"Bearer {toks[n]}"}
    assert c.post("/api/eval-grants", json=spec(), headers=H("rita")).status_code == 403
    r = c.post("/api/eval-grants", json=spec(created_by="mallory"), headers=H("bob"))
    assert r.status_code == 200 and r.json()["created_by"] == "bob"
    gid = r.json()["grant_id"]
    assert c.post(f"/api/eval-grants/{gid}/revoke", headers=H("carol")).status_code == 200
    assert c.post(f"/api/eval-grants/{gid}/revoke", headers=H("carol")).status_code == 409
    demo = TestClient(create_app(tmp / "data3", agent_cfg=cfg))
    assert demo.post("/api/eval-grants", json=spec()).status_code == 403


def test_normal_runtime_approval_unchanged(tmp_path):
    a = QAAgent(tmp_path)
    rid = a.start_run("Plan PCC-DEMO-5", story_key="PCC-DEMO-5", wait=True)
    a.approve(rid, "qa.reviewer", "approved", wait=True)
    assert a.approvals.latest(rid)["auth_mode"] == "demo" and a.store.get_run(rid)["status"] in ("completed", "failed")


# ---------------- Task 1: manual review with deferred execution + resumable first plan ----------------
def test_review_plan_rules(env):
    a, P, *_ = env
    rid = planned(a)
    ph = a.store.get_run(rid)["plan_hash"]
    with pytest.raises(ConflictError, match="cannot approve"):
        a.review_plan(rid, P["rita"], "approved", ph)                       # unauthorized approver
    with pytest.raises(ConflictError, match="authenticated"):
        a.review_plan(rid, Principal(name=None, role="approver", mode="demo"), "approved", ph)
    with pytest.raises(ConflictError, match="stale or missing plan hash"):
        a.review_plan(rid, P["bob"], "approved", "0" * 64)                  # altered / different plan
    a.store.update_run(rid, requested_by="bob")
    with pytest.raises(ConflictError, match="own run"):
        a.review_plan(rid, P["bob"], "approved", ph)
    a.store.update_run(rid, requested_by=None)
    out = a.review_plan(rid, P["bob"], "approved", ph)
    assert out["decision"] == "approved" and a.store.get_run(rid)["status"] == "approved"   # execution deferred
    assert sum(r["tool"] == "plan.review" and r["decision"] == "deny" for r in a.store.audit_rows(rid)) == 4


def test_execute_approved_refuses_altered_plan(env):
    a, P, *_ = env
    rid = planned(a)
    a.review_plan(rid, P["bob"], "approved", a.store.get_run(rid)["plan_hash"])
    pj = a.store.get_run(rid)["plan_json"]; pj["test_cases"][0]["params"]["sku"] = "SKU-9999"
    a.store.update_run(rid, plan_json=pj)                                    # plan altered after approval
    a.execute_approved(rid, wait=True)
    assert a.store.get_run(rid)["status"] == "blocked" and a.store.results(rid) == []


def harness():
    spec_ = importlib.util.spec_from_file_location("eval_b2", ROOT / "scripts/eval_b2.py")
    m = importlib.util.module_from_spec(spec_); spec_.loader.exec_module(m); return m


def cli(*args, token=None):
    import os
    env_ = dict(os.environ, QA_TOKEN=token or "")
    return subprocess.run([sys.executable, "-m", "qa_agent.eval_authorization", *args], cwd=ROOT, capture_output=True, text=True, env=env_)


@pytest.fixture
def stubkey(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "stub-not-a-real-key-000000")


def first_pause(tmp_path):
    rc = harness().main(["--provider", "stub", "--out", str(tmp_path), "--stories", "PCC-DEMO-5", "--runs", "1"])
    rep = json.loads((tmp_path / "eval_report.json").read_text())
    assert rc == 4 and rep["verdict"] == "PAUSED/AWAITING_FIRST_PLAN_APPROVAL"
    st = json.loads((tmp_path / "eval_state.json").read_text())["first_plan"]
    assert st["plan_hash"] == rep["first_plan"]["plan_hash"]
    return st


@pytest.fixture
def users(tmp_path):
    us = UserStore(tmp_path / "users.json")
    return {"bob": us.add("bob", "approver"), "rita": us.add("rita", "requester")}, tmp_path / "users.json"


@pytest.mark.integration
@pytest.mark.eval
def test_resumable_first_plan_approval_success_and_restart(tmp_path, stubkey, users):
    toks, uf = users
    out = tmp_path / "ev"
    st = first_pause(out)
    rc2 = harness().main(["--provider", "stub", "--out", str(out), "--stories", "PCC-DEMO-5", "--runs", "1"])   # restart: still paused
    assert rc2 == 4 and json.loads((out / "eval_state.json").read_text())["first_plan"]["run_id"] == st["run_id"]  # no new first plan
    r = cli("review", "--data-dir", str(out / "data"), "--users-file", str(uf), "--run", st["run_id"], "--plan-hash", st["plan_hash"],
            "--decision", "approved", token=toks["bob"])
    assert r.returncode == 0, r.stderr
    rc3 = harness().main(["--provider", "stub", "--out", str(out), "--stories", "PCC-DEMO-5", "--runs", "1"])        # resume
    rep = json.loads((out / "eval_report.json").read_text())
    assert rep["first_plan"]["approved_by"] == "bob" and rep["verdict"] == "SMOKE/NOT_EVALUABLE"   # subset => smoke, never PASS
    first = [x for x in rep["runs"] if x["first_plan_human_reviewed"]]
    assert len(first) == 1 and first[0]["run_id"] == st["run_id"] and first[0]["execution"]["status"] == "completed"


@pytest.mark.integration
@pytest.mark.eval
def test_resumable_first_plan_rejection(tmp_path, stubkey, users):
    toks, uf = users
    out = tmp_path / "ev"
    st = first_pause(out)
    assert cli("review", "--data-dir", str(out / "data"), "--users-file", str(uf), "--run", st["run_id"], "--plan-hash", st["plan_hash"],
               "--decision", "rejected", token=toks["bob"]).returncode == 0
    rc = harness().main(["--provider", "stub", "--out", str(out), "--stories", "PCC-DEMO-5", "--runs", "1"])
    rep = json.loads((out / "eval_report.json").read_text())
    assert rc == 5 and rep["verdict"] == "NOT_EVALUABLE" and "rejected by bob" in rep["first_plan"]["reason"]


@pytest.mark.integration
@pytest.mark.eval
def test_first_plan_unauthorized_or_altered(tmp_path, stubkey, users):
    toks, uf = users
    out = tmp_path / "ev"
    st = first_pause(out)
    base = ["review", "--data-dir", str(out / "data"), "--users-file", str(uf), "--run", st["run_id"], "--decision", "approved"]
    assert cli(*base, "--plan-hash", st["plan_hash"], token=toks["rita"]).returncode == 1          # requester, not approver
    assert cli(*base, "--plan-hash", st["plan_hash"], token="bob.wrong").returncode == 2           # bad token
    assert cli(*base, "--plan-hash", "f" * 64, token=toks["bob"]).returncode == 1                  # different plan
    rc = harness().main(["--provider", "stub", "--out", str(out), "--stories", "PCC-DEMO-5", "--runs", "1"])
    assert rc == 4                                                                                  # still paused, nothing auto-approved
    # approve, then tamper with the stored plan before resuming
    assert cli(*base, "--plan-hash", st["plan_hash"], token=toks["bob"]).returncode == 0
    a = QAAgent(out / "data", agent_cfg=harness().agent_config("replay"))
    pj = a.store.get_run(st["run_id"])["plan_json"]; pj["test_cases"][0]["title"] = "tampered"
    a.store.update_run(st["run_id"], plan_json=pj)
    rc = harness().main(["--provider", "stub", "--out", str(out), "--stories", "PCC-DEMO-5", "--runs", "1"])
    rep = json.loads((out / "eval_report.json").read_text())
    assert rc == 3 and "altered" in rep["first_plan"]["reason"]


def test_simulated_review_refused_for_compatible(tmp_path):
    with pytest.raises(SystemExit, match="simulated review"):
        harness().main(["--provider", "compatible", "--confirm-live-approved", "--self-test-simulated-review", "--out", str(tmp_path)])


# ---------------- Task 3: eval-only profile ----------------
@pytest.mark.cost
def test_eval_profile_max_retries_only_in_harness():
    glob_cfg = load_json(ROOT / "config/agent.json")
    assert glob_cfg["providers"]["compatible"]["max_retries"] == 3                               # global default unchanged
    assert glob_cfg["providers"]["compatible"]["enabled"] is False
    prof = load_json(ROOT / "config/eval_profile.json")
    assert prof["providers"]["compatible"]["max_retries"] == 1
    assert (prof["budget"]["per_run_usd"], prof["budget"]["daily_usd"], prof["budget"]["total_usd"]) == (1.0, 15.0, 15.0)
    h = harness()
    assert h.agent_config("stub", "http://127.0.0.1:1")["providers"]["compatible"]["max_retries"] == 1
    assert h.eval_profile_overrides(copy.deepcopy(glob_cfg))["providers"]["compatible"]["max_retries"] == 1
    assert h.eval_profile_overrides(copy.deepcopy(glob_cfg))["providers"]["compatible"]["enabled"] is False   # profile never enables compatible


@pytest.mark.security
def test_sealed_gold_not_tracked_and_not_in_ci():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout
    assert "PCC-DEMO-7.gold.json" not in tracked and "bluemeteor-sealed" not in tracked
    wf = (ROOT / ".github/workflows/qa-agent-ci.yml").read_text()
    assert "sealed" not in wf and "PCC-DEMO-7.gold" not in wf
