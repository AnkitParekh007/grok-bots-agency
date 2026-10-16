"""Review/dispatch drift and frozen-byte regression coverage; no live inference."""
import copy
import json
import shutil
from unittest.mock import patch

import pytest

from qa_agent import ROOT
from qa_agent.auth import Principal
from qa_agent.execution_package import digest
from qa_agent.runtime import QAAgent, ConflictError
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.security, pytest.mark.approval, pytest.mark.execution]


@pytest.fixture
def package(tmp_path):
    oracle_dir, fixture_dir = tmp_path / "oracles", tmp_path / "fixtures"
    shutil.copytree(ROOT / "fixtures/oracles", oracle_dir)
    shutil.copytree(ROOT / "fixtures/data", fixture_dir)
    cfg = agent_cfg("replay")
    cfg["oracles_dir"], cfg["fixture_data_dir"] = str(oracle_dir), str(fixture_dir)
    a = QAAgent(tmp_path / "state", agent_cfg=cfg)
    rid = a.start_run("Read the synthetic supplier import story", story_key="PCC-DEMO-1", requested_by="requester", wait=True)
    r = a.store.get_run(rid)
    assert r["status"] == "awaiting_approval", r["error"]
    yield a, rid, oracle_dir, fixture_dir
    a.store.db.close()


def review(a, rid):
    r = a.store.get_run(rid)
    a.review_plan(rid, Principal("reviewer", "approver", "local"), "approved", r["plan_hash"])


@pytest.mark.parametrize("change", ["oracle", "fixture", "policy", "configuration", "environment", "source", "plan", "snapshot", "scope", "stored_repository", "acceptance", "model_identity"])
def test_changed_inputs_cannot_execute_under_old_approval(package, change, monkeypatch):
    a, rid, oracle_dir, fixture_dir = package
    review(a, rid)
    if change == "oracle":
        p = oracle_dir / "PCC-DEMO-1.expected.json"
        obj = json.loads(p.read_text());obj["review"] = {"status": "changed"};p.write_text(json.dumps(obj))
    elif change == "fixture":
        p = next(fixture_dir.glob("*.csv"));p.write_bytes(p.read_bytes() + b"\nchanged")
    elif change == "policy":
        a.policy["tools"]["http.request"]["methods"].append("DELETE")
    elif change == "configuration":
        a.cfg["execution"]["api_timeout_ms"] = 30000
    elif change == "environment":
        monkeypatch.setenv("SYNTHETIC_SUT_URL", "http://127.0.0.1:1")
    elif change == "source":
        old = a.tracker.get_issue
        def changed(key):
            issue = copy.deepcopy(old(key));issue["fields"]["description"] += "\nchanged source";return issue
        monkeypatch.setattr(a.tracker, "get_issue", changed)
    elif change == "plan":
        r = a.store.get_run(rid);r["plan_json"]["test_cases"][0]["title"] = "changed case"
        a.store.update_run(rid, plan_json=r["plan_json"])
    elif change == "snapshot":
        a.artifacts.path_for(rid, "inputs/oracles.json").write_bytes(b"{}")
    elif change == "scope":
        a.store.update_run(rid, tenant_id="different-tenant")
    elif change == "stored_repository":
        a.store.update_run(rid, repo_context_json={"readme": "changed context"})
    elif change == "acceptance":
        a.store.update_run(rid, ac_json=[{"id": "changed", "text": "changed requirement"}])
    elif change == "model_identity":
        a.store.update_run(rid, model_id="unreviewed-model")
    with patch("qa_agent.runtime.PlaywrightExecutor.run") as execute:
        a.execute_approved(rid, wait=True)
        execute.assert_not_called()
    assert a.store.get_run(rid)["status"] == "blocked"
    assert any(r["decision"] == "deny" for r in a.store.audit_rows(rid))


def test_changed_inputs_are_rejected_before_review_recorded(package):
    a, rid, oracle_dir, _ = package
    p = oracle_dir / "PCC-DEMO-1.expected.json";p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(ConflictError, match="oracle_sha256"):
        review(a, rid)
    assert a.approvals.latest(rid) is None


def test_approval_hash_binds_more_than_test_cases(package):
    a, rid, _, _ = package
    r = a.store.get_run(rid)
    assert r["plan_hash"] == digest(r)
    assert r["plan_hash"] != r["execution_envelope_json"]["test_plan_sha256"]
    assert r["execution_envelope_json"]["code_sha256"]


def test_executor_receives_snapshot_bytes_not_mutable_fixture_files(package):
    a, rid, _, fixture_dir = package
    review(a, rid)
    originals = {p.name: p.read_bytes() for p in fixture_dir.glob("*.csv")}
    seen = []
    def capture(executor, *args, **kwargs):
        for p in fixture_dir.glob("*.csv"):
            p.write_bytes(b"changed after dispatch")
        assert executor.fixture_bytes == originals
        seen.append(True)
        return []  # Unit capture, not real SUT execution or quality evidence.
    with patch("qa_agent.runtime.PlaywrightExecutor.run", new=capture):
        a.execute_approved(rid, wait=True)
    assert seen == [True]


def test_manual_reviewer_loads_only_configuration_bound_to_review_hash(package):
    from qa_agent.execution_package import review_configuration, PackageChanged
    a, rid, _, _ = package
    run = a.store.get_run(rid)
    cfg = review_configuration(a.store.data_dir, rid, run["plan_hash"])
    assert cfg == a.cfg
    reviewer = QAAgent(a.store.data_dir, agent_cfg=cfg, review_only=True)
    try:
        reviewer.review_plan(rid, Principal("reviewer", "approver", "local"), "approved", run["plan_hash"])
        with pytest.raises(ConflictError, match="review-only"):
            reviewer.execute_approved(rid)
        with pytest.raises(ConflictError, match="review-only"):
            reviewer.start_run("attempted inference")
    finally:
        reviewer.store.db.close()
    p = a.artifacts.path_for(rid, "inputs/config.json")
    cfg["budget"]["per_run_usd"] = 100
    p.write_text(json.dumps(cfg))
    with pytest.raises(PackageChanged, match="configuration snapshot changed"):
        review_configuration(a.store.data_dir, rid, run["plan_hash"])
