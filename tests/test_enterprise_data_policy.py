"""Data-owner/model grants with synthetic corp-shaped input and MockTransport only."""
import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from qa_agent.runtime import QAAgent
from qa_agent.providers.models import DataClassDenied
from qa_agent.providers.local import OllamaProvider
from qa_agent.planning import build_messages, validate_plan, PolicyViolation
from qa_agent.sources import issue_bundle
from tests.b1_helpers import agent_cfg
from tests.test_free_providers import cfg as provider_cfg, response, SCHEMA
from tests.test_b1_planning_hardening import load, SK, FILES

pytestmark = [pytest.mark.provider, pytest.mark.planning, pytest.mark.security]


@pytest.fixture
def setup(tmp_path):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    pc = provider_cfg("ollama", region="test-local", allowed_data_classes=["synthetic", "internal", "confidential"])
    rule = {"approved": True, "approved_by": "synthetic-data-owner", "approval_ref": "TEST-ONLY", "expires_at": future,
            "tenant_id": "local", "classification": "internal", "fields": ["summary", "description", "customfield_123"],
            "acceptance_criteria_field": "customfield_123"}
    grant = {"approved": True, "approved_by": "synthetic-model-owner", "approval_ref": "TEST-ONLY", "expires_at": future,
             "tenant_id": "local", "project": "PCC", "provider": "ollama", "model": pc["model"], "endpoint": pc["base_url"],
             "purpose": "plan_story", "data_classes": ["internal"], "region": "test-local",
             "retention_decision": "Synthetic test declaration", "training_use_decision": "Synthetic test declaration"}
    cfg = agent_cfg("replay")
    cfg["projects"]["PCC"]["source_policy"] = rule
    cfg["model_data_grants"] = [grant]
    agent = QAAgent(tmp_path, agent_cfg=cfg)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response("ollama"))
    model = OllamaProvider(pc, gateway=agent.gateway, transport=httpx.MockTransport(handle))
    agent.store.create_run("data-run", "Synthetic data policy test", "synthetic-local", "ollama", "mock protocol")
    agent.store.update_run("data-run", project="PCC")
    context = {"story_key": "PCC-1", "data_class": "unclassified", "catalog": {}, "story": {"key": "PCC-1", "fields": {
        "summary": "Synthetic organization-shaped story", "description": "Acceptance criteria\n1. Description requirement is superseded by configured AC field.",
        "customfield_123": "1. The approved custom requirement is present.", "secret_field": "DO_NOT_SEND"}}}
    yield agent, model, context, calls
    agent.store.db.close()


def prepared(setup):
    agent, model, context, _ = setup
    return agent.data_policy.prepare(agent.store.get_run("data-run"), context, model)


def test_approved_exact_model_gets_minimized_pinned_source_without_permit_in_prompt(setup):
    agent, model, _, calls = setup
    context = prepared(setup)
    assert "secret_field" not in context["story"]["fields"]
    assert context["source_bundle"]["requirements"][0]["text"] == "The approved custom requirement is present."
    result = model.generate("plan_story", context, SCHEMA, run_id="data-run")
    assert result.text == '{"ok":true}' and len(calls) == 1
    body = calls[0].content.decode()
    assert "DO_NOT_SEND" not in body and context["data_permit"] not in body
    assert "untrusted_source_bundle" in body and "source_sha256" in body


@pytest.mark.parametrize("mutation", ["unclassified", "missing_source_approval", "missing_grant", "wrong_model", "wrong_endpoint", "wrong_region", "expired", "synthetic_label"])
def test_missing_or_mismatched_approvals_deny_before_network(setup, mutation):
    agent, model, context, calls = setup
    rule = agent.projects["PCC"]["source_policy"]
    grant = agent.data_policy.grants[0]
    if mutation == "missing_source_approval": rule["approved"] = False
    elif mutation == "missing_grant": agent.data_policy.grants.clear()
    elif mutation == "wrong_model": grant["model"] = "other-model"
    elif mutation == "wrong_endpoint": grant["endpoint"] = "http://127.0.0.1:9999"
    elif mutation == "wrong_region": grant["region"] = "somewhere-else"
    elif mutation == "expired": grant["expires_at"] = "2020-01-01T00:00:00+00:00"
    elif mutation == "synthetic_label": rule["classification"] = "synthetic"
    with pytest.raises(DataClassDenied):
        if mutation == "unclassified":
            model.generate("plan_story", context, SCHEMA, run_id="data-run")
        else:
            prepared(setup)
    assert calls == []


@pytest.mark.parametrize("mutation", ["description", "classification", "synthetic", "grant_revoked", "project", "permit_missing"])
def test_permit_cannot_be_reused_with_changed_input_scope_or_approval(setup, mutation):
    agent, model, _, calls = setup
    context = prepared(setup)
    if mutation == "description": context["story"]["fields"]["description"] += " changed"
    elif mutation == "classification": context["data_class"] = "confidential"
    elif mutation == "synthetic": context["data_class"] = "synthetic"
    elif mutation == "grant_revoked": agent.data_policy.grants[0]["approved"] = False
    elif mutation == "project": agent.store.update_run("data-run", project="OTHER")
    elif mutation == "permit_missing": context.pop("data_permit")
    with pytest.raises(DataClassDenied):
        model.generate("plan_story", context, SCHEMA, run_id="data-run")
    assert not calls


def test_custom_ac_quote_must_cite_its_own_source_and_content_version(setup):
    agent, _, _, _ = setup
    plan, desc, oracles = load("PCC-DEMO-1")
    plan = copy.deepcopy(plan)
    text = "\n".join(f"{index}. {ac['source_quote']}" for index, ac in enumerate(plan["acceptance_criteria"], 1))
    story = {"key": "PCC-DEMO-1", "fields": {"summary": "Synthetic", "description": "1. Another description requirement.", "customfield_123": text}}
    bundle = issue_bundle(story, agent.projects["PCC"]["source_policy"])
    for ac, evidence in zip(plan["acceptance_criteria"], bundle["requirements"]):
        ac.update(source_id=evidence["source_id"], source_sha256=evidence["source_sha256"])
    assert validate_plan(plan, story["fields"]["description"], SK, oracles, FILES, source_bundle=bundle)
    plan["acceptance_criteria"][0]["source_quote"] = "Another description requirement."
    with pytest.raises(PolicyViolation, match="source_quote"):
        validate_plan(plan, story["fields"]["description"], SK, oracles, FILES, source_bundle=bundle)
    plan["acceptance_criteria"][0]["source_quote"] = bundle["requirements"][0]["text"]
    plan["acceptance_criteria"][0]["source_sha256"] = "0" * 64
    with pytest.raises(PolicyViolation, match="authoritative source"):
        validate_plan(plan, desc, SK, oracles, FILES, source_bundle=bundle)


def test_runtime_persists_grounded_bundle_and_invalidates_tampered_snapshot(setup, monkeypatch):
    from qa_agent.auth import Principal
    from qa_agent.runtime import ConflictError
    agent, model, _, _ = setup
    plan, _, _ = load("PCC-DEMO-1")
    custom = "\n".join(f"{index}. {ac['source_quote']}" for index, ac in enumerate(plan["acceptance_criteria"], 1))
    story = {"key": "PCC-DEMO-1", "fields": {"summary": "Synthetic organization-shaped test", "description": "Description is contextual.", "customfield_123": custom}}
    agent.tracker.kind = "jira_cloud"  # Explicitly exercises the non-synthetic classification path.
    agent.tracker.ac_field = "customfield_123"
    monkeypatch.setattr(agent.tracker, "get_issue", lambda key: copy.deepcopy(story))
    from tests.test_enterprise_domains import approval
    approval(agent, {"tenant_id": "local", "project": "PCC", "repository": None,
                     "story_key": "PCC-DEMO-1", "environment": "synthetic-local"}, agent.envs._base("synthetic-local"))
    monkeypatch.setattr(model, "prepare", lambda: None)  # No real engine or metadata probe.
    def handler(request):
        body = json.loads(request.content)
        user = json.loads(body["messages"][1]["content"])
        corpus = user["untrusted_source_bundle"]
        recorded = copy.deepcopy(plan)
        for ac, evidence in zip(recorded["acceptance_criteria"], corpus["requirements"]):
            ac.update(source_id=evidence["source_id"], source_sha256=evidence["source_sha256"])
        return httpx.Response(200, json=response("ollama", message={"content": json.dumps(recorded)}, _qa_stub=True))
    model.transport = httpx.MockTransport(handler)
    # Provider's HTTP client is constructed lazily by _post and reads this transport.
    agent.model = model
    rid = agent.start_run("Grounding contract using a fixture answer", story_key="PCC-DEMO-1", project="PCC", requested_by="requester", wait=True)
    run = agent.store.get_run(rid)
    assert run["status"] == "awaiting_approval", run["error"]
    assert run["source_bundle_json"]["requirements"][0]["source_id"] == run["ac_json"]["acceptance_criteria"][0]["source_id"]
    assert "STUB" in run["model_label"]
    agent.artifacts.path_for(rid, "inputs/source-bundle.json").write_text('{}')
    with pytest.raises(ConflictError, match="source bundle snapshot changed"):
        agent.review_plan(rid, Principal("reviewer", "approver", "local"), "approved", run["plan_hash"])
