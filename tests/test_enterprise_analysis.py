"""Unsupported stories remain useful without inventing a plan or running a model/SUT."""
import json
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from qa_agent.runtime import QAAgent, ConflictError
from qa_agent.server import create_app
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.planning, pytest.mark.security, pytest.mark.approval]


@pytest.fixture
def analysis_agent(tmp_path):
    cfg = agent_cfg("replay")
    cfg["oracles_dir"] = str(tmp_path / "no-oracles")
    agent = QAAgent(tmp_path / "state", agent_cfg=cfg)
    yield agent
    agent.store.db.close()


def test_missing_oracles_produce_source_review_without_model_or_environment_execution(analysis_agent):
    agent = analysis_agent
    with patch.object(agent.model, "generate") as model, patch.object(agent.envs, "resolve") as target, \
            patch("qa_agent.runtime.PlaywrightExecutor.run") as execute:
        rid = agent.start_run("Analyze the source", story_key="PCC-DEMO-1", project="PCC", requested_by="requester", wait=True)
        model.assert_not_called();target.assert_not_called();execute.assert_not_called()
    run = agent.store.get_run(rid)
    assert run["status"] == "analysis_only", run["error"]
    assert run["plan_json"] is None and run["plan_hash"] is None and run["execution_envelope_json"] is None
    assert run["analysis_json"]["tests_executed"] == run["analysis_json"]["model_calls"] == 0
    assert len(run["ac_json"]["acceptance_criteria"]) == 5
    assert len(run["analysis_json"]["coverage_gaps"]) == 5
    assert agent.store.results(rid) == [] and agent.approvals.latest(rid) is None
    with pytest.raises(ConflictError):
        agent.approve(rid, "reviewer", "approved")
    with pytest.raises(ConflictError):
        agent.execute_approved(rid)
    report = json.loads(agent.artifacts.path_for(rid, "report.json").read_text())
    assert report["execution_eligible"] is False and report["quality_claim_eligible"] is False
    sources = json.loads(agent.artifacts.path_for(rid, "inputs/source-fields.json").read_text())
    for ac in report["acceptance_criteria"]:
        assert ac["source_quote"] in sources[ac["source_id"]]["text"]
        assert ac["line_start"] >= 1


def test_custom_ac_field_has_separate_verbatim_source_and_html_is_escaped(analysis_agent, monkeypatch):
    agent = analysis_agent
    agent.tracker.ac_field = "customfield_10001"
    source = {"key": "PCC-1", "fields": {"summary": "<script>alert(1)</script>", "description": "No AC heading",
              "customfield_10001": "1. A configured requirement is present.\n2. <img src=x onerror=alert(1)> is plain data."}}
    monkeypatch.setattr(agent.tracker, "get_issue", lambda key: source)
    rid = agent.start_run("Review source", story_key="PCC-1", wait=True)
    run = agent.store.get_run(rid)
    assert run["status"] == "analysis_only", run["error"]
    criteria = run["ac_json"]["acceptance_criteria"]
    assert len(criteria) == 2 and all(a["source"] == "customfield_10001" for a in criteria)
    page = agent.artifacts.path_for(rid, "report.html").read_text()
    assert "<script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page


def test_unstructured_source_reports_need_for_human_requirement_identification(analysis_agent, monkeypatch):
    agent = analysis_agent
    monkeypatch.setattr(agent.tracker, "get_issue", lambda key: {"key": key, "fields": {"description": "An ambiguous request"}})
    rid = agent.start_run("Review source", story_key="PCC-1", wait=True)
    review = agent.store.get_run(rid)["analysis_json"]
    assert review["acceptance_criteria"] == [] and review["coverage_gaps"][0]["kind"] == "source_review_required"


def test_source_review_files_use_existing_authenticated_run_artifact_boundary(tmp_path):
    from qa_agent.auth import UserStore
    users = UserStore(tmp_path / "users.json")
    requester = users.add("owner", "requester")
    outsider = users.add("other", "viewer")
    cfg = agent_cfg("replay", auth={"mode": "local"})
    cfg["oracles_dir"] = str(tmp_path / "no-oracles")
    app = create_app(tmp_path / "state", agent_cfg=cfg, users_file=users.path)
    agent = app.state.agent
    try:
        rid = agent.start_run("Review source", story_key="PCC-DEMO-1", requested_by="owner", wait=True)
        with TestClient(app) as client:
            assert client.get(f"/api/runs/{rid}/report.json", headers={"Authorization": "Bearer " + requester}).json()["mode"] == "analysis_only"
            assert client.get(f"/api/runs/{rid}/report.json", headers={"Authorization": "Bearer " + outsider}).status_code == 404
    finally:
        agent.store.db.close()
