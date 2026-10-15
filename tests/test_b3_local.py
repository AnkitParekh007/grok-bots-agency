"""B3 offline contracts; native responses are mocked, never real reasoning evidence."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from qa_agent import ROOT
from qa_agent.auth import Principal, UserStore
from qa_agent.runtime import QAAgent, ConflictError
from qa_agent.util import load_json
from scripts import local_model_b3 as B3
from scripts import eval_b2 as B2

pytestmark = [pytest.mark.provider, pytest.mark.approval, pytest.mark.eval, pytest.mark.security]


@pytest.fixture
def local(tmp_path, monkeypatch):
    cfg = copy.deepcopy(load_json(ROOT / "config/agent.json"))
    cfg["auth"]["mode"] = "local"
    cfg["providers"]["ollama"].update(model="qwen3:1.7b", max_retries=0)
    requests = []
    recorded = load_json(ROOT / "fixtures/replay/PCC-DEMO-5.json")["responses"]["plan_story"]
    def handle(r):
        requests.append(r.url.path)
        payload = {"/api/version": {"version": "MOCK-RUNTIME"},
                   "/api/tags": {"models": [{"name": "qwen3:1.7b", "digest": "MOCK-DIGEST", "size": 100}]},
                   "/api/show": {"capabilities": ["completion"], "model_info": {"qwen3.context_length": 32768},
                                 "details": {"quantization_level": "TEST-Q4"}, "template": "MOCK TEMPLATE"},
                   "/api/ps": {"models": []},
                   "/api/chat": {"model": "qwen3:1.7b", "message": {"content": json.dumps(recorded)},
                                 "done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 200,
                                 "eval_duration": 2000000000}}[r.url.path]
        return httpx.Response(200, json=payload)
    def factory(*args, **kwargs):
        a = QAAgent(*args, **kwargs)
        a.model.transport = httpx.MockTransport(handle)
        return a
    monkeypatch.setattr(B3, "QAAgent", factory)
    monkeypatch.setattr(B3, "hardware", lambda: {"scope": "MOCK HARDWARE"})
    monkeypatch.setattr(B3, "sampling", lambda out: __import__('contextlib').nullcontext())
    users = UserStore(tmp_path / "users.json")
    token = users.add("requester", "requester")
    monkeypatch.setenv("QA_TOKEN", token)
    return cfg, tmp_path / "out", users, requests


@pytest.mark.parametrize("change", ["remote", "cloud", "real_tracker", "real_git"])
def test_configuration_refuses_remote_cloud_and_corporate_sources(tmp_path, monkeypatch, change):
    cfg = load_json(ROOT / "config/agent.json")
    if change == "remote": cfg["providers"]["ollama"]["base_url"] = "https://example.com"
    if change == "cloud": cfg["providers"]["ollama"]["model"] = "example:cloud"
    if change == "real_tracker": cfg["issue_tracker"]["provider"] = "jira_cloud"
    if change == "real_git": cfg["git"]["provider"] = "bitbucket_cloud"
    p = tmp_path / "config.json"; p.write_text(json.dumps(cfg))
    monkeypatch.setenv("QA_AGENT_CONFIG", str(p))
    with pytest.raises(ValueError): B3.configuration()


def test_diagnostic_uses_native_metadata_only_and_preserves_unknown_memory(local):
    cfg, out, _, requests = local
    _, meta = B3.diagnose(cfg, out)
    assert meta["inference"]["ready"] and meta["inference"]["generation_performed"] is False
    assert meta["inference"]["details"]["quantization_level"] == "TEST-Q4"
    assert meta["inference"]["memory"]["memory_bytes"] is None
    assert set(requests) == {"/api/version", "/api/tags", "/api/show", "/api/ps"}
    assert "/api/chat" not in requests and "/api/pull" not in requests


def test_plan_is_owned_pauses_and_recorded_mock_never_qualifies(local):
    cfg, out, users, requests = local
    code, report = B3.plan(cfg, out, users.path, "defect_on")
    assert code == 4 and report["stage"] == "awaiting_approval"
    assert report["requester"] == "requester" and report["schema_catalog_valid"]
    assert report["authenticated_human_approval"] is False and report["results"] == []
    assert report["real_reasoning_evidence"] is False and report["quality"]["contaminated"]
    assert report["verdict"] == "SMOKE/NOT_EVALUABLE" and report["audit_ok"]
    assert requests.count("/api/chat") == 1
    a = B3.QAAgent(out / "data", agent_cfg=cfg)
    with pytest.raises(ConflictError, match="own run"):
        a.review_plan(report["run_id"], Principal("requester", "approver", "local"), "approved", report["plan_hash"])
    with pytest.raises(ValueError, match="already used"):
        B3.plan(cfg, out, users.path, "defect_on")


def test_plan_denies_demo_and_nonrequester_before_inference(local, monkeypatch):
    cfg, out, users, requests = local
    cfg["auth"]["mode"] = "demo"
    with pytest.raises(ValueError, match="auth.mode=local"): B3.plan(cfg, out, users.path, "defect_on")
    cfg["auth"]["mode"] = "local"
    monkeypatch.setenv("QA_TOKEN", users.add("approver", "approver"))
    with pytest.raises(ValueError, match="local requester"): B3.plan(cfg, out, users.path, "defect_on")
    assert not requests


def test_unavailable_runtime_never_generates_or_invents_plan(local, monkeypatch):
    cfg, out, users, requests = local
    monkeypatch.setattr(B3, "ollama_metadata", lambda p: {"ready": False, "error": "unavailable", "generation_performed": False})
    code, report = B3.plan(cfg, out, users.path, "defect_on")
    assert code == 3 and report["run_id"] is None and report["verdict"] == "NOT_EVALUABLE"
    assert not (out / "b3_attempt.json").exists() and not requests


def test_metadata_tampering_cannot_relabel_an_approved_phase(local):
    cfg, out, users, _ = local
    B3.plan(cfg, out, users.path, "defect_on")
    a = load_json(out / "b3_attempt.json"); a["phase"] = "defect_off"; B3.write(out / "b3_attempt.json", a)
    with pytest.raises(ValueError, match="audited binding"): B3.summarize(cfg, out)


def test_benchmark_refuses_unapproved_or_recorded_smoke(local):
    cfg, out, users, _ = local
    B3.plan(cfg, out, users.path, "defect_on")
    with pytest.raises(ValueError, match="distinct-human-approved"): B3.verified_smoke(cfg, out, "defect_on")


def test_b2_requester_cli_refuses_fake_tokens_and_approver_role(local, monkeypatch):
    _, out, users, _ = local
    monkeypatch.setenv("QA_TOKEN", "not-a-token")
    args = ["--provider", "ollama", "--confirm-live-approved", "--requester-users-file", str(users.path), "--out", str(out)]
    with pytest.raises(SystemExit, match="authenticate a local requester"): B2.main(args)
    monkeypatch.setenv("QA_TOKEN", users.add("reviewer", "approver"))
    with pytest.raises(SystemExit, match="authenticate a local requester"): B2.main(args)


def test_b2_first_plan_binds_authenticated_requester_and_refuses_owner_change(local):
    cfg, out, users, _ = local
    agent = B3.QAAgent(out / "data", agent_cfg=cfg)
    args = SimpleNamespace(requested_by="requester")
    _, pause = B2.first_plan_stage(agent, args, out, ["PCC-DEMO-5"], False)
    assert pause["exit"] == 4
    run = agent.store.get_run(pause["run_id"])
    assert run["requested_by"] == "requester" and not agent.approvals.latest(run["id"])
    _, denied = B2.first_plan_stage(agent, SimpleNamespace(requested_by="another_requester"), out, ["PCC-DEMO-5"], False)
    assert denied["verdict"] == "NOT_EVALUABLE" and "requester changed" in denied["reason"]


def test_b2_resume_refuses_a_changed_pinned_model_digest(local):
    cfg, out, _, _ = local
    agent = B3.QAAgent(out / "data", agent_cfg=cfg)
    args = SimpleNamespace(requested_by="requester")
    B2.first_plan_stage(agent, args, out, ["PCC-DEMO-5"], False)
    agent.model.version = "A-DIFFERENT-DIGEST"
    _, denied = B2.first_plan_stage(agent, args, out, ["PCC-DEMO-5"], False)
    assert denied["verdict"] == "NOT_EVALUABLE" and "version changed" in denied["reason"]


def test_b2_mixed_model_revisions_cannot_form_a_complete_benchmark():
    runs = [{"planned": True, "model_provider": "ollama", "model_id": "small", "model_version": "digest-1"},
            {"planned": True, "model_provider": "ollama", "model_id": "small", "model_version": "digest-2"}]
    assert not B2.pinned_identity_matches(runs, "ollama", "small", "digest-1")
    runs[1]["model_version"] = "digest-1"
    assert B2.pinned_identity_matches(runs, "ollama", "small", "digest-1")


def test_missing_or_tampered_evidence_does_not_qualify(tmp_path):
    import hashlib
    results = [{"screenshot": "screen.png", "trace": "trace.zip", "log": "log.txt"}]
    assert not B3.evidence_status(tmp_path, results)["ok"]
    artifacts = []
    for path in results[0].values():
        data = b'OFFLINE TEST ARTIFACT'
        (tmp_path / path).write_bytes(data)
        artifacts.append({"path": path, "sha256": hashlib.sha256(data).hexdigest()})
    raw = json.dumps({"artifacts": artifacts}).encode()
    (tmp_path / 'report.json').write_bytes(raw)
    (tmp_path / 'report.json.sha256').write_text(hashlib.sha256(raw).hexdigest())
    (tmp_path / 'report.html').write_text('OFFLINE REPORT')
    assert B3.evidence_status(tmp_path, results)["ok"]
    (tmp_path / 'screen.png').write_bytes(b'TAMPERED')
    assert not B3.evidence_status(tmp_path, results)["ok"]


def test_later_grant_enablement_does_not_change_smoke_planning_identity(local):
    cfg, _, _, _ = local
    original = B3.identity(cfg)["configuration_sha256"]
    cfg["unattended_eval_approval"]["enabled"] = True
    assert B3.identity(cfg)["configuration_sha256"] == original
    cfg["repair"]["max_format_repairs"] += 1
    assert B3.identity(cfg)["configuration_sha256"] != original
