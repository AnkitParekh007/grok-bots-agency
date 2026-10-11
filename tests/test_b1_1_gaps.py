"""B1.1 gap closure: contracts, catalog versioning, correlation IDs, error normalization, jitter, extra validation,
plan expiry/ownership, audit sequence/export, budget edge cases. No network beyond 127.0.0.1."""
import copy, json, subprocess, sqlite3, sys
from datetime import datetime, timedelta, timezone
import httpx, jsonschema, pytest
from fastapi.testclient import TestClient
from qa_agent import ROOT, contracts as C
from qa_agent.auth import UserStore
from qa_agent.planning import (PLAN_STORY_SCHEMA, validate_plan, evaluate_plan, catalog_version, FormatError, PolicyViolation)
from qa_agent.providers.models import GrokProvider, ModelCallError, ProviderConfigError, compose_plan_story
from qa_agent.runtime import QAAgent, ConflictError
from qa_agent.server import create_app
from qa_agent.skills import LocalSkillRegistry
from qa_agent.store import Store, verify_export
from qa_agent.util import load_json, sha256, canon
from tests.b1_helpers import grok_cfg, agent_cfg, fake_key, model_stub, wait_status

SK = LocalSkillRegistry()
FILES = sorted(p.name for p in (ROOT / "fixtures/data").glob("*.csv"))
PROMPT = "Read the synthetic supplier import story, generate test cases, execute the approved tests, and give me the results."
STORY_KEYS = ["PCC-DEMO-1", "PCC-DEMO-2", "PCC-DEMO-3", "PCC-DEMO-4"]


def rec(key):
    return compose_plan_story(load_json(ROOT / f"fixtures/replay/{key}.json"))


def ctx(key):
    return load_json(ROOT / f"fixtures/jira/{key}.json")["fields"]["description"], load_json(ROOT / f"fixtures/oracles/{key}.expected.json")


@pytest.fixture
def key(monkeypatch):
    k = fake_key(); monkeypatch.setenv("XAI_API_KEY", k); return k


# ---------------- planning: contracts & catalog ----------------
@pytest.mark.planning
def test_named_contracts_compose_wire_schema():
    assert PLAN_STORY_SCHEMA is C.TestPlan
    for k in STORY_KEYS:
        p = rec(k)
        jsonschema.validate(p, C.TestPlan)
        for a in p["acceptance_criteria"]:
            jsonschema.validate(a, C.AcceptanceCriterion)
        for b in p["behaviors"]:
            jsonschema.validate(b, C.Requirement)
        for t in p["test_cases"]:
            jsonschema.validate(t, C.TestScenario); jsonschema.validate(t["oracle_ref"], C.OracleReference)
        for g in p["coverage_gaps"]:
            jsonschema.validate(g, C.CoverageGap)
        jsonschema.validate(p["selected_skills"], C.SkillSelection)


@pytest.mark.planning
def test_evaluate_plan_returns_plan_validation_result():
    desc, orc = ctx("PCC-DEMO-2")
    ok = evaluate_plan(json.dumps(rec("PCC-DEMO-2")), "stop", desc, SK, orc, FILES)
    bad_fmt = evaluate_plan("{", "stop", desc, SK, orc, FILES)
    p = rec("PCC-DEMO-2"); p["test_cases"][0]["skill"] = "nope"
    bad_pol = evaluate_plan(json.dumps(p), "stop", desc, SK, orc, FILES)
    for r in (ok, bad_fmt, bad_pol):
        jsonschema.validate(r, C.PlanValidationResult)
    assert (ok["category"], bad_fmt["category"], bad_pol["category"]) == ("ok", "format", "policy") and len(ok["plan_hash"]) == 64


@pytest.mark.planning
def test_catalog_version_is_content_addressed(monkeypatch):
    v1 = catalog_version(SK)
    assert v1.startswith("qa-action-catalog/1+") and v1 == catalog_version(LocalSkillRegistry())
    sk2 = LocalSkillRegistry()
    sk2.skills["pim_import_testing"].params = {**sk2.skills["pim_import_testing"].params, "api_import": {"file": {"kind": "any"}}}
    assert catalog_version(sk2) != v1


def mut(fn, key="PCC-DEMO-1"):
    p = copy.deepcopy(rec(key)); fn(p)
    desc, orc = ctx(key)
    return lambda: validate_plan(p, desc, SK, orc, FILES)


@pytest.mark.planning
@pytest.mark.security
@pytest.mark.parametrize("name,fn,exc,match", [
    ("unregistered_endpoint_param", lambda p: p["test_cases"][0]["params"].update(file="/api/admin/purge"), PolicyViolation, "disallowed"),
    ("endpoint_key", lambda p: p["test_cases"][0].update(endpoint="/api/admin/purge"), PolicyViolation, "restricted key"),
    ("wrong_param_type", lambda p: p["test_cases"][0]["params"].update(file=123), FormatError, "schema"),
    ("params_not_object", lambda p: p["test_cases"][0].update(params=["supplier_valid.csv"]), FormatError, "schema"),
    ("duplicate_test_id", lambda p: p["test_cases"].append(dict(p["test_cases"][1], id=p["test_cases"][0]["id"])), FormatError, "duplicate test id"),
    ("missing_step_action", lambda p: p["test_cases"][0].pop("action"), FormatError, "schema"),
    ("missing_step_skill", lambda p: p["test_cases"][0].pop("skill"), FormatError, "schema"),
    ("missing_step_params", lambda p: p["test_cases"][0].pop("params"), FormatError, "schema"),
    ("approval_bypass_key", lambda p: p.update(skip_approval=True), PolicyViolation, "restricted key"),
    ("approval_bypass_in_test", lambda p: p["test_cases"][0].update(requires_approval=False), PolicyViolation, "restricted key"),
    ("production_env_key", lambda p: p["test_cases"][0].update(environment="production"), PolicyViolation, "restricted key"),
    ("production_host_param", lambda p: p["test_cases"][0]["params"].update(file="https://pcc.prod.example/x.csv"), PolicyViolation, "disallowed"),
    ("production_word_param", lambda p: p["test_cases"][1]["params"].update(sku="PROD"), PolicyViolation, "disallowed"),
    ("base_url_key", lambda p: p["understanding"].update(base_url="https://pcc.example.com"), PolicyViolation, "restricted key"),
])
def test_additional_validation(name, fn, exc, match):
    with pytest.raises(exc, match=match):
        mut(fn)()


# ---------------- provider ----------------
class Script:
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def __call__(self, req):
        self.calls.append(req)
        it = self.items.pop(0)
        if it == "timeout":
            raise httpx.ReadTimeout("t", request=req)
        if it == "nonjson":
            return httpx.Response(200, text="<html>oops</html>")
        status, body = it
        return httpx.Response(status, json=body)


def ok():
    b = load_json(ROOT / "fixtures/model_recordings/chat_completion_ok.json"); b["choices"][0]["message"]["content"] = "{}"; return b


def prov(agent, s, **over):
    return GrokProvider(grok_cfg("http://127.0.0.1:9/v1", **over), gateway=agent.gateway, transport=httpx.MockTransport(s), sleep=lambda x: None)


CTX = {"story_key": "PCC-DEMO-2", "data_class": "synthetic", "story": load_json(ROOT / "fixtures/jira/PCC-DEMO-2.json"), "catalog": {}}


@pytest.mark.provider
def test_correlation_ids_sent_and_audited(tmp_path, key):
    a = QAAgent(tmp_path)
    s = Script((503, {"e": 1}), (200, ok()))
    r = prov(a, s).generate("plan_story", CTX, {}, run_id="run-c1")
    sent = [q.headers["x-request-id"] for q in s.calls]
    assert r.correlation_ids == sent and len(set(sent)) == 2 and all(x.startswith("run-c1-") for x in sent)
    audited = [json.loads(x["args_summary"])["correlation_id"] for x in a.store.audit_rows("run-c1") if x["tool"] == "model.request"]
    assert audited == sent


@pytest.mark.provider
@pytest.mark.parametrize("items,kind,status,retryable", [
    ([(429, {})] * 4, "rate_limited", 429, True), ([(503, {})] * 4, "server_error", 503, True),
    ([(401, {})], "auth", 401, False), ([(403, {})], "auth", 403, False), ([(404, {})], "not_found", 404, False),
    ([(400, {})], "bad_request", 400, False), ([(422, {})], "bad_request", 422, False),
    (["timeout"] * 4, "timeout", None, True), (["nonjson"], "invalid_response", 200, False)])
def test_provider_error_normalization(tmp_path, key, items, kind, status, retryable):
    a = QAAgent(tmp_path)
    with pytest.raises(ModelCallError) as e:
        prov(a, Script(*items)).generate("plan_story", CTX, {}, run_id="r")
    d = e.value.as_dict()
    assert (d["kind"], d["status"], d["retryable"]) == (kind, status, retryable) and d["correlation_id"].startswith("r-")
    assert key not in json.dumps(d)


@pytest.mark.provider
def test_backoff_jitter_bounded_and_capped(tmp_path, key):
    p = prov(QAAgent(tmp_path), Script(), backoff_base_s=1.0, max_backoff_s=5.0, backoff_jitter_ratio=0.25)
    p.rng.seed(7)
    vals = [p.backoff(0) for _ in range(50)]
    assert all(0.75 <= v <= 1.25 for v in vals) and len({round(v, 6) for v in vals}) > 10   # jittered, not constant
    assert all(p.backoff(10) <= 5.0 for _ in range(20))                                       # capped


@pytest.mark.provider
@pytest.mark.integration
def test_model_timeout_at_runtime_is_audited(tmp_path, key):
    with model_stub() as stub:
        cfg = agent_cfg("grok", stub.base); cfg["providers"]["grok"].update(timeout_s=0.5, max_retries=0)
        stub.queue([{"sleep": 2}])
        a = QAAgent(tmp_path, agent_cfg=cfg)
        run = a.store.get_run(a.start_run("Plan PCC-DEMO-2", wait=True))
        assert run["status"] == "failed" and "timeout" in run["error"]
        rows = [r for r in a.store.audit_rows(run["id"]) if r["tool"] == "model.request"]
        assert len(rows) == 1 and "ReadTimeout" in rows[0]["outcome"]
        assert any(t["kind"] == "model_call_failed:timeout" for t in a.store.q("SELECT kind FROM telemetry"))


@pytest.mark.planning
@pytest.mark.integration
def test_approval_bypass_from_model_rejected_and_audited(tmp_path, key):
    with model_stub() as stub:
        p = rec("PCC-DEMO-2"); p["skip_approval"] = True
        stub.queue([{"content": json.dumps(p)}])
        a = QAAgent(tmp_path, agent_cfg=agent_cfg("grok", stub.base))
        run = a.store.get_run(a.start_run("Plan PCC-DEMO-2", wait=True))
        assert run["status"] == "failed" and "skip_approval" in run["error"] and len(stub.requests()) == 1
        assert any(r["tool"] == "model.output_validation" and r["decision"] == "deny" for r in a.store.audit_rows(run["id"]))


# ---------------- approval ----------------
@pytest.mark.approval
def test_plan_expiry(tmp_path):
    a = QAAgent(tmp_path)
    rid = a.start_run(PROMPT, wait=True)
    old = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
    a.store.update_run(rid, plan_created_at=old)
    with pytest.raises(ConflictError, match="expired"):
        a.approve(rid, "x", "approved")
    assert a.approvals.latest(rid) is None
    assert any(r["tool"] == "plan.approve" and "expired" in r["reason"] for r in a.store.audit_rows(rid))


@pytest.mark.approval
@pytest.mark.execution
def test_stale_action_catalog_blocks_execution(tmp_path):
    a = QAAgent(tmp_path)
    rid = a.start_run(PROMPT, wait=True)
    run = a.store.get_run(rid)
    assert run["catalog_version"] == catalog_version(a.skills)
    a.approvals.record(rid, run["plan_hash"], "x", "approved")
    a.store.update_run(rid, catalog_version="qa-action-catalog/1+0000000000000000")
    a.execute(rid, wait=True)
    assert a.store.get_run(rid)["status"] == "blocked" and a.store.results(rid) == []
    assert any(r["tool"] == "plan.execute" and r["reason"] == "stale action catalog" for r in a.store.audit_rows(rid))


@pytest.mark.approval
@pytest.mark.security
def test_run_ownership_for_cancel(tmp_path):
    us = UserStore(tmp_path / "u.json")
    t = {n: us.add(n, r) for n, r in [("rita", "requester"), ("ron", "requester"), ("bob", "approver")]}
    c = TestClient(create_app(tmp_path / "d", auth_mode="local", users_file=tmp_path / "u.json"))
    H = lambda n: {"Authorization": f"Bearer {t[n]}"}
    rid = c.post("/api/chat", json={"message": PROMPT}, headers=H("rita")).json()["run_id"]
    wait_status(lambda: c.get(f"/api/runs/{rid}", headers=H("rita")).json(), {"awaiting_approval"})
    assert c.post(f"/api/runs/{rid}/cancel", headers=H("ron")).status_code == 403
    assert c.post(f"/api/runs/{rid}/cancel", headers=H("rita")).status_code == 200
    rid2 = c.post("/api/chat", json={"message": PROMPT}, headers=H("ron")).json()["run_id"]
    wait_status(lambda: c.get(f"/api/runs/{rid2}", headers=H("ron")).json(), {"awaiting_approval"})
    assert c.post(f"/api/runs/{rid2}/cancel", headers=H("bob")).status_code == 200      # approver may cancel any run
    assert c.get(f"/api/runs/{rid2}", headers=H("bob")).json()["status"] == "cancelled"


# ---------------- audit ----------------
def seeded_store(tmp_path):
    a = QAAgent(tmp_path)
    a.start_run(PROMPT, wait=True)
    return a


@pytest.mark.audit
def test_sequence_numbers_and_canonical_payload_hash(tmp_path):
    a = seeded_store(tmp_path)
    rows = a.store.q("SELECT * FROM audit ORDER BY id")
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    r = rows[0]
    shuffled = {k: r[k] for k in reversed(Store.AUDIT_FIELDS)}
    assert Store.payload_hash(shuffled) == r["payload_hash"] == sha256(canon({k: r[k] for k in Store.AUDIT_FIELDS}))
    jl = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [x["seq"] for x in jl] == [r["seq"] for r in rows] and jl[-1]["entry_hash"] == rows[-1]["entry_hash"]


@pytest.mark.audit
@pytest.mark.parametrize("tamper,reason", [("skip", "incorrect sequence"), ("dup", "incorrect sequence"), ("swap", "incorrect sequence")])
def test_incorrect_sequence_detected(tmp_path, tamper, reason):
    a = seeded_store(tmp_path)
    db = sqlite3.connect(tmp_path / "qa_agent.db")
    db.execute("DROP TRIGGER audit_no_update")
    if tamper == "skip":
        db.execute("UPDATE audit SET seq = seq + 5 WHERE seq = 3")
    elif tamper == "dup":
        db.execute("UPDATE audit SET seq = 2 WHERE seq = 3")
    else:
        db.execute("UPDATE audit SET seq = CASE seq WHEN 2 THEN 3 WHEN 3 THEN 2 END WHERE seq IN (2,3)")
    db.commit()
    v = a.store.verify_audit_chain()
    assert not v["ok"] and reason in v["reason"]


@pytest.mark.audit
def test_export_and_export_verification(tmp_path):
    a = seeded_store(tmp_path)
    out = tmp_path / "export.jsonl"
    run = lambda *args: subprocess.run([sys.executable, "-m", "qa_agent.audit_verify", *args], cwd=ROOT, capture_output=True, text=True)
    r = run("export", "--data-dir", str(tmp_path), "--out", str(out))
    assert r.returncode == 0 and json.loads(r.stdout)["count"] == len(a.store.q("SELECT id FROM audit"))
    r = run("verify-export", str(out))
    assert r.returncode == 0 and json.loads(r.stdout)["ok"]
    lines = out.read_text().splitlines()
    # 1) edit a row only -> body hash mismatch
    row = json.loads(lines[1]); row["decision"] = "deny"
    bad = lines[:]; bad[1] = canon(row); (tmp_path / "bad1.jsonl").write_text("\n".join(bad) + "\n")
    assert verify_export(tmp_path / "bad1.jsonl")["reason"].startswith("body hash mismatch")
    # 2) edit a row AND recompute the manifest body hash -> chain verification still catches it
    man = json.loads(bad[-1]); body = "".join(l + "\n" for l in bad[:-1]); man["_manifest"]["body_sha256"] = sha256(body)
    bad[-1] = canon(man); (tmp_path / "bad2.jsonl").write_text("\n".join(bad) + "\n")
    v = verify_export(tmp_path / "bad2.jsonl")
    assert not v["ok"] and "entry_hash mismatch" in v["reason"]
    # 3) drop the manifest
    (tmp_path / "bad3.jsonl").write_text("\n".join(lines[:-1]) + "\n")
    assert verify_export(tmp_path / "bad3.jsonl")["reason"] == "missing manifest"


@pytest.mark.audit
def test_export_endpoint(tmp_path):
    c = TestClient(create_app(tmp_path))
    rid = c.post("/api/chat", json={"message": PROMPT}).json()["run_id"]
    wait_status(lambda: c.get(f"/api/runs/{rid}").json(), {"awaiting_approval"})
    r = c.get("/api/audit/export")
    assert r.status_code == 200 and "_manifest" in r.text.splitlines()[-1]


# ---------------- cost ----------------
@pytest.mark.cost
def test_agent_refuses_unknown_pricing(tmp_path, key):
    cfg = agent_cfg("grok", "http://127.0.0.1:9/v1")
    cfg["providers"]["grok"]["pricing"] = {"input_per_mtok_usd": None, "cached_input_per_mtok_usd": None, "output_per_mtok_usd": None}
    with pytest.raises(ProviderConfigError, match="pricing not configured"):
        QAAgent(tmp_path, agent_cfg=cfg)


@pytest.mark.cost
@pytest.mark.integration
def test_missing_usage_never_invented(tmp_path, key):
    with model_stub() as stub:
        stub.queue([{"no_usage": True}])
        a = QAAgent(tmp_path, agent_cfg=agent_cfg("grok", stub.base))
        rid = a.start_run("Plan PCC-DEMO-2", wait=True)
        c = a.store.run_usage(rid)["calls"][0]
        assert c["outcome"] == "usage_missing" and c["priced"] == 0
        assert all(c[k] is None for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"))
        assert c["cost_usd"] > 0          # conservative estimate reserved against the budget, flagged unpriced


@pytest.mark.cost
def test_cost_is_usage_times_configured_price(tmp_path, key):
    s = Script((200, ok()))
    r = prov(QAAgent(tmp_path), s, pricing={"input_per_mtok_usd": 1.0, "cached_input_per_mtok_usd": 0.0, "output_per_mtok_usd": 10.0}).generate("plan_story", CTX, {}, run_id="r")
    assert r.cost_usd == pytest.approx((1000 * 1.0 + 1400 * 10.0) / 1e6)
