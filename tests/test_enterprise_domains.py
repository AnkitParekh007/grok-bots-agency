"""Domain onboarding authority tests with fixture data only; no real qualification."""
import copy
import json
import shutil
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from qa_agent import ROOT
from qa_agent.auth import Principal
from qa_agent.domain import authorize, describe, DomainNotApproved, FORMAT
from qa_agent.runtime import QAAgent, ConflictError
from qa_agent.util import canon, sha256
from scripts.domain_proposal import proposal, main
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.planning, pytest.mark.approval, pytest.mark.security]


@pytest.fixture
def domain(tmp_path):
    cfg = agent_cfg("replay")
    for key, source in (("oracles_dir", "fixtures/oracles"), ("fixture_data_dir", "fixtures/data")):
        target = tmp_path / key
        shutil.copytree(ROOT / source, target)
        cfg[key] = str(target)
    agent = QAAgent(tmp_path / "state", agent_cfg=cfg)
    story = agent.tracker.get_issue("PCC-DEMO-1")
    agent.tracker.kind = "jira_cloud"  # Mock fixture, but must take the organization approval path.
    run = {"tenant_id": "local", "project": "PCC", "repository": None,
           "story_key": "PCC-DEMO-1", "environment": "synthetic-local"}
    env = agent.envs._base(run["environment"])
    yield agent, run, env, story
    agent.close()
    agent.store.db.close()


def approval(agent, run, env):
    record = {"format": FORMAT, "id": "synthetic-contract-only", "version": "1", "enabled": True, "approved": True,
              "approved_by": "domain-owner", "approval_ref": "TEST-ONLY", "oracle_author": "author",
              "oracle_reviewer": "reviewer", "independence_ref": "TEST-ONLY-INDEPENDENCE",
              "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
              "inputs_sha256": sha256(canon(describe(agent, run, env)))}
    agent.projects[run["project"]]["domain_capabilities"] = [record]
    return record


def test_unsupported_domain_with_oracle_still_produces_analysis_without_model_or_target(domain):
    agent, _, _, _ = domain
    with patch.object(agent.model, "generate") as model, patch.object(agent.envs, "resolve") as target, \
            patch("qa_agent.runtime.PlaywrightExecutor.run") as execute:
        rid = agent.start_run("Review source", story_key="PCC-DEMO-1", project="PCC", wait=True)
        model.assert_not_called(); target.assert_not_called(); execute.assert_not_called()
    run = agent.store.get_run(rid)
    assert run["status"] == "analysis_only", run["error"]
    assert run["plan_hash"] is None and run["analysis_json"]["model_calls"] == 0
    assert len([row for row in agent.store.audit_rows(rid) if row["tool"] == "tracker.get_issue"]) == 1


def test_exact_reviewed_inputs_produce_receipt_without_changing_other_gates(domain):
    agent, run, env, story = domain
    record = approval(agent, run, env)
    assert authorize(agent, run, env, story)["approval"] == record
    # Domain review cannot grant model access to an organization source.
    rid = agent.start_run("Review source", story_key=run["story_key"], project="PCC", wait=True)
    actual = agent.store.get_run(rid)
    assert actual["status"] == "failed" and "DataClassDenied" in actual["error"]
    assert agent.store.results(rid) == []


@pytest.mark.parametrize("change", ["missing", "disabled", "unapproved", "expired", "naive_expiry", "bad_expiry", "same_reviewer",
                                    "missing_independence", "wrong_digest", "unknown_field", "duplicate", "wrong_tenant", "wrong_repo",
                                    "wrong_environment", "oracle", "fixture", "catalog", "action_source", "oversized_records"])
def test_domain_approval_denials(domain, change, monkeypatch):
    agent, run, env, story = domain
    record = approval(agent, run, env)
    pc = agent.projects["PCC"]
    if change == "missing": pc.pop("domain_capabilities")
    elif change == "disabled": record["enabled"] = False
    elif change == "unapproved": record["approved"] = False
    elif change == "expired": record["expires_at"] = "2020-01-01T00:00:00+00:00"
    elif change == "naive_expiry": record["expires_at"] = "2099-01-01T00:00:00"
    elif change == "bad_expiry": record["expires_at"] = 23
    elif change == "same_reviewer": record["oracle_reviewer"] = " AUTHOR "
    elif change == "missing_independence": record["independence_ref"] = ""
    elif change == "wrong_digest": record["inputs_sha256"] = "0" * 64
    elif change == "unknown_field": record["grant_all"] = True
    elif change == "duplicate": pc["domain_capabilities"].append(copy.deepcopy(record))
    elif change == "wrong_tenant": run["tenant_id"] = "other"
    elif change == "wrong_repo": run["repository"] = "bluemeteorapps/supplierportalui"
    elif change == "wrong_environment": env["base_url"] = "https://unreviewed.example.invalid"
    elif change == "oracle": agent.oracle_path(run["story_key"]).write_bytes(b"{}")
    elif change == "fixture": next((ROOT / agent.cfg["fixture_data_dir"]).glob("*.csv")).write_bytes(b"changed")
    elif change == "catalog": monkeypatch.setattr(agent.skills, "list", lambda: [])
    elif change == "action_source":
        module = next(iter(agent.skills.skills.values()))
        name = next(iter(module.actions))
        # Copy the global mapping before replacing an action in this agent.
        monkeypatch.setattr(module, "actions", dict(module.actions))
        module.actions[name] = ("api", lambda ctx, params: None)
    elif change == "oversized_records": pc["domain_capabilities"] = [record] * 101
    with pytest.raises((DomainNotApproved, ValueError)):
        authorize(agent, run, env, story)


def test_synthetic_label_or_loopback_alone_does_not_bypass_domain_review(domain):
    agent, run, env, story = domain
    assert "synthetic" in story["fields"]["labels"]
    with pytest.raises(DomainNotApproved):
        authorize(agent, run, env, story)
    agent.tracker.kind = "mock_jira"
    assert authorize(agent, run, env, story)["mode"] == "synthetic_harness"
    env.update(kind="test", base_url="https://supplier.pcctest.bluemeteor.com/", status="verified")
    with pytest.raises(DomainNotApproved):
        authorize(agent, run, env, story)


def test_revocation_checked_before_review_sources_or_execution(domain, monkeypatch):
    agent, run, env, _ = domain
    # Produce the existing synthetic plan, then require a reviewed domain for it.
    agent.tracker.kind = "mock_jira"
    rid = agent.start_run("Synthetic plan", story_key=run["story_key"], project="PCC", requested_by="requester", wait=True)
    stored = agent.store.get_run(rid)
    assert stored["status"] == "awaiting_approval", stored["error"]
    agent.tracker.kind = "jira_cloud"
    with patch.object(agent.tracker, "get_issue") as read, patch("qa_agent.runtime.PlaywrightExecutor.run") as execute:
        with pytest.raises(ConflictError, match="domain approval"):
            agent.review_plan(rid, Principal("reviewer", "approver", "local"), "approved", stored["plan_hash"])
        read.assert_not_called(); execute.assert_not_called()
    assert agent.approvals.latest(rid) is None


@pytest.mark.parametrize("change", ["expired", "revoked", "oracle_drift"])
def test_reviewed_domain_receipt_is_bound_and_rechecked_at_dispatch(domain, change):
    from qa_agent import execution_package as EP
    agent, run, env, _ = domain
    agent.tracker.kind = "mock_jira"
    rid = agent.start_run("Fixture plan for domain-envelope contract", story_key=run["story_key"], project="PCC", requested_by="requester", wait=True)
    agent.tracker.kind = "jira_cloud"
    record = approval(agent, run, env)
    # Explicit internal unit harness: rebind fixture plan, without model/SUT calls.
    EP.capture(agent, rid, env)
    stored = agent.store.get_run(rid)
    assert stored["execution_envelope_json"]["domain_approval"]["approval"] == record
    agent.review_plan(rid, Principal("reviewer", "approver", "local"), "approved", stored["plan_hash"])
    if change == "expired": record["expires_at"] = "2020-01-01T00:00:00+00:00"
    elif change == "revoked": record["approved"] = False
    else: agent.oracle_path(run["story_key"]).write_bytes(b"{}")
    with patch("qa_agent.runtime.PlaywrightExecutor.run") as execute:
        agent.execute_approved(rid, wait=True)
        execute.assert_not_called()
    assert agent.store.get_run(rid)["status"] == "blocked"


def test_domain_expiry_before_model_attempt_prevents_generation(domain, monkeypatch):
    agent, run, env, story = domain
    record = approval(agent, run, env)
    agent.store.create_run("phase-gate", "Unit gate fixture", run["environment"], "replay", "Fixture")
    agent.store.update_run("phase-gate", **{k: v for k, v in run.items() if k != "environment"}, story_json=story)
    # This deliberately isolates the domain gate from the separately tested data gate.
    monkeypatch.setattr(agent.data_policy, "verify", lambda *a, **kw: record.update(expires_at="2020-01-01T00:00:00+00:00"))
    with patch.object(agent.model, "generate") as generate:
        with pytest.raises(DomainNotApproved):
            agent._model_plan("phase-gate", {}, story["fields"]["description"], agent._oracles(run["story_key"]))
        generate.assert_not_called()


def test_fixture_inventory_bounds_are_checked_before_approval(domain):
    agent, run, env, _ = domain
    root = ROOT / agent.cfg["fixture_data_dir"]
    for i in range(257): (root / f"bounded-{i}.csv").write_bytes(b"fixture")
    with pytest.raises(DomainNotApproved, match="count exceeds"):
        describe(agent, run, env)


def test_proposal_is_disabled_and_local_only_and_never_overwrites(domain, tmp_path, capsys):
    agent, run, _, _ = domain
    environments = json.loads((ROOT / "config/environments.json").read_text())
    result = proposal(agent.cfg, environments, project="PCC", story=run["story_key"], repository=None, environment="synthetic-local")
    assert result["execution_eligible"] is False
    assert result["approval"]["approved"] is False and result["approval"]["enabled"] is False
    assert result["approval"]["inputs_sha256"] == sha256(canon(result["inputs"]))
    cfg_path = tmp_path / "agent.json"
    cfg_path.write_text(json.dumps(agent.cfg))
    out = tmp_path / "proposal.json"
    args = ["--config", str(cfg_path), "--project", "PCC", "--story", run["story_key"], "--environment", "synthetic-local", "--out", str(out)]
    main(args)
    assert json.loads(out.read_text()) == result
    assert "SUT calls: 0" in capsys.readouterr().out
    with pytest.raises(FileExistsError): main(args)
    with pytest.raises(ValueError):
        proposal(agent.cfg, environments, project="PCC", story=run["story_key"], repository="other/repo", environment="synthetic-local")
