import copy, json
import jsonschema, pytest
from qa_agent import ROOT
from qa_agent.providers.models import ReplayModelProvider, GrokProvider, ProviderNotApproved, build_model_provider
from qa_agent.runtime import QAAgent, PlanInvalid
from qa_agent.schemas import AC_SCHEMA, PLAN_SCHEMA
from qa_agent.util import load_json


def test_replay_outputs_validate():
    m = ReplayModelProvider(ROOT / "fixtures/replay")
    assert m.label == "REPLAYED (mock model)"
    jsonschema.validate(m.complete("extract_acceptance_criteria", {"story_key": "PCC-DEMO-1"}), AC_SCHEMA)
    jsonschema.validate(m.complete("generate_test_plan", {"story_key": "PCC-DEMO-1"}), PLAN_SCHEMA)


@pytest.mark.parametrize("mutate", [
    lambda p: p["test_cases"][0].pop("oracle_ref"),
    lambda p: p["test_cases"][0].update(type="shell"),
    lambda p: p["test_cases"][0].update(extra="x"),
    lambda p: p["test_cases"][0]["params"].update(file={"nested": 1}),
])
def test_invalid_plan_rejected_by_schema(mutate):
    plan = copy.deepcopy(load_json(ROOT / "fixtures/replay/PCC-DEMO-1.json")["responses"]["generate_test_plan"])
    mutate(plan)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(plan, PLAN_SCHEMA)


def test_invalid_model_output_fails_run_without_plan(tmp_path, monkeypatch):
    rec = load_json(ROOT / "fixtures/replay/PCC-DEMO-1.json")
    rec["responses"]["generate_test_plan"]["test_cases"][0]["skill"] = "shell_exec"   # unregistered skill
    d = tmp_path / "replay"; d.mkdir()
    (d / "PCC-DEMO-1.json").write_text(json.dumps(rec))
    agent = QAAgent(tmp_path / "data")
    agent.model.dir = d
    rid = agent.start_run("Read PCC-DEMO-1", wait=True)
    run = agent.store.get_run(rid)
    assert run["status"] == "failed" and "not registered" in run["error"] and run["plan_json"] is None


def test_hallucinated_ac_rejected(tmp_path):
    rec = load_json(ROOT / "fixtures/replay/PCC-DEMO-1.json")
    rec["responses"]["extract_acceptance_criteria"]["acceptance_criteria"][0]["source_quote"] = "Supports XLSX files"
    d = tmp_path / "replay"; d.mkdir()
    (d / "PCC-DEMO-1.json").write_text(json.dumps(rec))
    agent = QAAgent(tmp_path / "data"); agent.model.dir = d
    run = agent.store.get_run(agent.start_run("Read PCC-DEMO-1", wait=True))
    assert run["status"] == "failed" and "source_quote" in run["error"]


def test_grok_disabled_fails_closed(monkeypatch):
    cfg = load_json(ROOT / "config/agent.json")
    monkeypatch.setenv("QA_MODEL_PROVIDER", "grok")
    with pytest.raises(ProviderNotApproved):
        build_model_provider(cfg, ROOT)
    # even "enabled" without approval ref + key fails closed
    with pytest.raises(ProviderNotApproved):
        GrokProvider(dict(cfg["providers"]["grok"], enabled=True, approved=True))
    monkeypatch.setenv("QA_MODEL_PROVIDER", "openai")
    with pytest.raises(ProviderNotApproved):
        build_model_provider(cfg, ROOT)


def test_agent_refuses_to_start_with_unapproved_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_MODEL_PROVIDER", "grok")
    with pytest.raises(ProviderNotApproved):
        QAAgent(tmp_path)
