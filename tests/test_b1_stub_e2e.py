"""Full pipeline with GrokProvider pointed at the LOCAL OpenAI-compatible stub (127.0.0.1). The stub answers from
hand-authored recordings: this exercises transport, schema, repair, policy, cost and execution - NOT model reasoning."""
import json, shutil, sqlite3, zipfile
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from qa_agent import ROOT
from qa_agent.providers.mock_jira import MockJiraProvider
from qa_agent.runtime import QAAgent
from qa_agent.server import create_app
from qa_agent.util import load_json
from tests.b1_helpers import agent_cfg, model_stub, fake_key, wait_status


@pytest.fixture
def key(monkeypatch):
    k = fake_key()
    monkeypatch.setenv("XAI_API_KEY", k)
    return k


@pytest.fixture
def stub():
    with model_stub() as s:
        yield s


def mk(tmp_path, stub, **over):
    return QAAgent(tmp_path, agent_cfg=agent_cfg("grok", stub.base, **over))


def plan(agent, key_, wait=True):
    rid = agent.start_run(f"Plan {key_}", wait=wait)
    return rid, agent.store.get_run(rid)


def audit_text(agent, rid):
    return json.dumps(agent.store.audit_rows(rid))


def test_new_story_via_stub_full_execution(tmp_path, stub, key, monkeypatch, sut_factory):
    with sut_factory("") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        a = mk(tmp_path, stub)
        rid, run = plan(a, "PCC-DEMO-2")
        assert run["status"] == "awaiting_approval", run["error"]
        assert "STUB ENDPOINT" in run["model_label"] and "NOT real model reasoning" in run["model_label"]
        pj = run["planning_json"]
        assert [g["ac_refs"] for g in pj["coverage_gaps"]] == [["AC3"]]
        assert {t["category"] for t in pj["test_cases"]} == {"positive", "negative"}
        req = stub.requests()
        assert len(req) == 1 and req[0]["response_format_type"] == "json_schema" and req[0]["strict"] is True and req[0]["auth_present"]
        u = a.store.run_usage(rid)["totals"]
        assert u["calls"] == 1 and u["total_tokens"] > 0 and u["cost_usd"] > 0     # from stub usage x configured (test) prices
        a.approve(rid, "qa.reviewer", "approved", wait=True)
        run = a.store.get_run(rid)
        assert run["status"] == "completed" and run["summary_json"]["passed"] == 3, a.store.results(rid)
        rep = load_json(a.artifacts.path_for(rid, "report.json"))
        assert rep["per_ac"]["AC3"] == "untested (coverage gap)" and rep["usage"]["cost_usd"] == u["cost_usd"]
        assert "STUB ENDPOINT" in rep["model"]["label"]


def test_edge_story_and_defect_detection_on_unseen_story(tmp_path, stub, key, monkeypatch, sut_factory):
    with sut_factory("all") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        a = mk(tmp_path, stub)
        rid, run = plan(a, "PCC-DEMO-4")
        assert {t["category"] for t in run["planning_json"]["test_cases"]} == {"edge"}
        a.approve(rid, "qa.reviewer", "approved", wait=True)
        assert a.store.get_run(rid)["summary_json"]["passed"] == 2
        rid3, run3 = plan(a, "PCC-DEMO-3")        # injection story, benign stub answer
        assert run3["status"] == "awaiting_approval"   # never auto-approved
        assert any(g["kind"] == "untrusted_instruction_ignored" for g in run3["planning_json"]["coverage_gaps"])
        a.approve(rid3, "qa.reviewer", "approved", wait=True)
        st = {r["id"]: r["status"] for r in a.store.results(rid3)}
        assert st == {"TC-01": "failed", "TC-02": "failed"}       # seeded drop_color detected on an unseen story
        assert "evil.example" not in audit_text(a, rid3)


def test_format_error_repaired_within_bound(tmp_path, stub, key):
    stub.queue([{"content": "this is not json"}, {"finish_reason": "length"}])
    a = mk(tmp_path, stub)
    rid, run = plan(a, "PCC-DEMO-2")
    assert run["status"] == "awaiting_approval", run["error"]
    reqs = stub.requests()
    assert len(reqs) == 3 and reqs[1]["n_messages"] == 3 and "FORMAT problem" in reqs[1]["messages"][-1]["content"]
    assert a.store.run_usage(rid)["totals"]["calls"] == 3
    denies = [r for r in a.store.audit_rows(rid) if r["tool"] == "model.output_validation"]
    assert len(denies) == 2 and all("format error" in d["reason"] for d in denies)


def test_format_error_repair_exhausted(tmp_path, stub, key):
    stub.queue([{"content": "nope"}] * 3)
    a = mk(tmp_path, stub)
    rid, run = plan(a, "PCC-DEMO-2")
    assert run["status"] == "failed" and "after 3 attempt(s)" in run["error"] and run["plan_json"] is None
    assert len(stub.requests()) == 3


def test_policy_violation_rejected_without_repair(tmp_path, stub, key):
    stub.queue([{"variant": "obey_injection"}])
    a = mk(tmp_path, stub)
    rid, run = plan(a, "PCC-DEMO-3")
    assert run["status"] == "failed" and "restricted key 'command'" in run["error"] and run["plan_json"] is None
    assert len(stub.requests()) == 1                                # no repair attempt
    d = [r for r in a.store.audit_rows(rid) if r["tool"] == "model.output_validation"]
    assert len(d) == 1 and d[0]["decision"] == "deny" and "not repairable" in d[0]["reason"]
    assert not any(r["tool"].startswith(("http.", "browser.")) for r in a.store.audit_rows(rid))


def test_429_via_stub_then_success(tmp_path, stub, key):
    stub.queue([{"status": 429, "headers": {"Retry-After": "0"}}, {"status": 503}])
    a = mk(tmp_path, stub)
    rid, run = plan(a, "PCC-DEMO-4")
    assert run["status"] == "awaiting_approval" and len(stub.requests()) == 3
    outcomes = [r["outcome"] for r in a.store.audit_rows(rid) if r["tool"] == "model.request"]
    assert outcomes[0].startswith("error: _HTTPStatus") and outcomes[-1] == "ok"


def test_missing_usage_fails_closed(tmp_path, stub, key):
    stub.queue([{"no_usage": True}])
    a = mk(tmp_path, stub)
    rid, run = plan(a, "PCC-DEMO-2")
    assert run["status"] == "failed" and "UsageMissing" in run["error"]
    c = a.store.run_usage(rid)["calls"][0]
    assert c["outcome"] == "usage_missing" and c["priced"] == 0 and c["cost_usd"] > 0   # charged at conservative estimate


def test_per_run_and_daily_budget_caps(tmp_path, stub, key):
    a = mk(tmp_path, stub, budget={"per_run_usd": 0.000001, "daily_usd": 150})
    rid, run = plan(a, "PCC-DEMO-2")
    assert run["status"] == "failed" and "per-run cap" in run["error"] and stub.requests() == []
    assert any(r["tool"] == "model.budget_gate" and r["decision"] == "deny" for r in a.store.audit_rows(rid))
    b = mk(tmp_path / "b", stub, budget={"per_run_usd": 10, "daily_usd": 1.0})
    b.store.add_usage("old-run", "grok", "grok-4.7", "plan_story", 0, {"total_tokens": 1}, 1.0, True, "stop")
    rid2, run2 = plan(b, "PCC-DEMO-2")
    assert run2["status"] == "failed" and "daily cap" in run2["error"] and stub.requests() == []


def test_unclassified_story_never_sent_to_model(tmp_path, stub, key):
    d = tmp_path / "jira"; d.mkdir()
    s = load_json(ROOT / "fixtures/jira/PCC-DEMO-2.json"); s["fields"]["labels"] = ["supplier-import"]   # no 'synthetic' label
    (d / "PCC-DEMO-2.json").write_text(json.dumps(s))
    a = mk(tmp_path / "data", stub)
    a.tracker = MockJiraProvider(d)
    rid, run = plan(a, "PCC-DEMO-2")
    assert run["status"] == "failed" and "DataClassDenied" in run["error"] and stub.requests() == []
    assert any(r["tool"] == "model.data_class_gate" and r["decision"] == "deny" for r in a.store.audit_rows(rid))


def test_model_failure_no_fallback(tmp_path, stub, key):
    stub.queue([{"status": 500}] * 4)
    a = mk(tmp_path, stub)
    rid, run = plan(a, "PCC-DEMO-2")
    assert run["status"] == "failed" and "HTTP 500" in run["error"]
    assert run["model_provider"] == "grok" and run["plan_json"] is None and "REPLAY" not in run["model_label"]


def test_secret_never_persisted_or_exposed(tmp_path, stub, key, monkeypatch, sut_factory):
    with sut_factory("all") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        app = create_app(tmp_path, agent_cfg=agent_cfg("grok", stub.base))
        c = TestClient(app)
        stub.queue([{"status": 401, "echo_auth": True}])
        r1 = c.post("/api/chat", json={"message": "Plan PCC-DEMO-2"}).json()["run_id"]
        bad = wait_status(lambda: c.get(f"/api/runs/{r1}").json(), {"failed"})
        assert "HTTP 401" in bad["error"] and "[REDACTED]" in bad["error"]
        r2 = c.post("/api/chat", json={"message": "Plan PCC-DEMO-1"}).json()["run_id"]
        run = wait_status(lambda: c.get(f"/api/runs/{r2}").json(), {"awaiting_approval", "failed"})
        c.post(f"/api/runs/{r2}/approval", json={"approver": "qa.reviewer", "decision": "approved"})
        wait_status(lambda: c.get(f"/api/runs/{r2}").json(), {"completed"})
        blobs = [c.get(u).content for u in ["/api/config", "/api/runs", f"/api/runs/{r1}", f"/api/runs/{r2}", "/api/telemetry",
                                             f"/api/runs/{r2}/report.html", f"/api/runs/{r2}/report.json", f"/api/runs/{r1}/usage"]]
        for p in tmp_path.rglob("*"):
            if p.is_file():
                blobs.append(p.read_bytes())
                if p.suffix == ".zip":
                    with zipfile.ZipFile(p) as z:
                        blobs.extend(z.read(n) for n in z.namelist())
        assert len(blobs) > 50
        assert not any(key.encode() in b for b in blobs), "API key leaked"
        assert any(b"model.request" in b for b in blobs)
