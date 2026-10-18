"""Manual-suite contracts using synthetic plans and constructed evidence.

The constructed evidence tests exercise gate semantics, not browser/SUT quality.
One real planning handoff proves admission precedes the existing approval gate.
"""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from qa_agent.auth import UserStore
from qa_agent.regression import RegressionSuites, SuiteDenied, contract, verify_bound_run
from qa_agent.report import write_report
from qa_agent.server import create_app
from qa_agent.runtime import QAAgent
from qa_agent.queued_runtime import QueuedRuntime
from qa_agent.recovery import backup, restore
from qa_agent.store import Store, SCHEMA_VERSION, RUN_UPDATE_FIELDS
from qa_agent import execution_package as EP
from qa_agent.comparator import compare
from qa_agent.util import canon, sha256
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.security, pytest.mark.approval, pytest.mark.execution, pytest.mark.audit]


@pytest.fixture(scope="module")
def planned_seed(tmp_path_factory):
    root = tmp_path_factory.mktemp("suite-seed")
    cfg = agent_cfg("replay", regression={"enabled": True}, auth={"login_attempts_per_minute": 100})
    app = create_app(root, auth_mode="local", users_file=root / "users.json", agent_cfg=cfg)
    agent = app.state.agent
    template = agent.start_run("Test PCC-DEMO-1", story_key="PCC-DEMO-1", project="PCC", wait=True, requested_by="requester")
    run = agent.store.get_run(template)
    assert run["status"] == "awaiting_approval", run.get("error")
    original_contract = contract(run)
    # Construct a gate-only vector covering the declared supported AC subset.
    # This is not the demo's full plan or a claim its intentional gaps are solved.
    cases = [run["plan_json"]["test_cases"][0]]
    covered = set(cases[0]["ac_refs"])
    criteria = [ac for ac in run["ac_json"]["acceptance_criteria"] if ac["id"] in covered]
    planning = copy.deepcopy(run["planning_json"])
    planning["acceptance_criteria"], planning["coverage_gaps"], planning["test_cases"] = criteria, [], cases
    source = {"fixture": "constructed gate-only source snapshot, never supplied to a model"}
    agent.store.update_run(template, ac_json={"acceptance_criteria": criteria, "gaps": []}, planning_json=planning,
                           plan_json={"test_cases": cases}, source_bundle_json=source)
    agent.artifacts.write_bytes(template, "inputs/source-bundle.json", canon(source).encode())
    EP.capture(agent, template, agent.envs.resolve(run["environment"]))
    yield cfg, agent.store.get_run(template), agent.artifacts.root / template / "inputs", original_contract
    agent.close();agent.store.db.close()


@pytest.fixture
def suites(tmp_path, planned_seed):
    users = UserStore(tmp_path / "users.json")
    tokens = {name: users.add(name, role) for name, role in
              [("requester", "requester"), ("reviewer", "approver"), ("other", "requester"), ("viewer", "viewer")]}
    cfg, seed, inputs, _ = planned_seed
    cfg = copy.deepcopy(cfg)
    app = create_app(tmp_path / "state", auth_mode="local", users_file=users.path, agent_cfg=cfg)
    agent, service = app.state.agent, app.state.regression
    receipts = {name: {"mode": "local_credential", "principal": vars(users.verify(token)),
                       "revision": app.state.sessions.revision(name)} for name, token in tokens.items()}
    template = seed["id"]
    agent.store.create_run(template, "constructed gate vector", seed["environment"], seed["model_provider"], seed["model_label"])
    agent.store.update_run(template, **{k: seed[k] for k in RUN_UPDATE_FIELDS if k in seed and k != "updated"})
    for path in inputs.rglob("*"):
        if path.is_file():
            agent.artifacts.write_bytes(template, "inputs/" + path.relative_to(inputs).as_posix(), path.read_bytes())
    run = agent.store.get_run(template)
    assert run["status"] == "awaiting_approval", run.get("error")
    agent.approvals.record(template, run["plan_hash"], "reviewer", "approved", identity="reviewer", auth_mode="local")

    def complete(run_id, *, status="passed"):
        if run_id != template:
            original = agent.store.get_run(template)
            keys = ("story_key environment model_provider model_label story_json ac_json plan_json plan_hash model_id model_version "
                    "planning_json catalog_version plan_created_at project repository repo_context_json execution_envelope_json source_bundle_json").split()
            agent.store.update_run(run_id, **{k: original[k] for k in keys})
            for path in (agent.artifacts.root / template / "inputs").rglob("*"):
                if path.is_file():
                    agent.artifacts.write_bytes(run_id, path.relative_to(agent.artifacts.root / template).as_posix(), path.read_bytes())
            agent.approvals.record(run_id, original["plan_hash"], "reviewer", "approved", identity="reviewer", auth_mode="local")
        run = agent.store.get_run(run_id)
        oracles = json.loads(agent.artifacts.path_for(run_id, "inputs/oracles.json").read_bytes())["oracles"]
        for case in run["plan_json"]["test_cases"]:
            oracle = oracles[case["oracle_ref"]]
            expected = copy.deepcopy(oracle["expected"])
            actual = copy.deepcopy(expected) if status == "passed" else {}
            compared = compare(expected, actual, oracle.get("tolerance", 0.0))
            result = {"id": case["id"], "title": case["title"], "ac_refs": case["ac_refs"], "status": status,
                      "oracle_ref": case["oracle_ref"], "expected": expected, "actual": actual,
                      "diffs": compared["diffs"], "error": None, "evidence_complete": True}
            for kind, filename in (("screenshot", "screenshot.png"), ("trace", "trace.zip"), ("log", "log.txt")):
                result[kind] = f"evidence/{case['id']}/{filename}"
                agent.artifacts.write_bytes(run_id, result[kind], b"constructed synthetic gate evidence")
            agent.store.save_result(run_id, result)
        n = len(run["plan_json"]["test_cases"])
        agent.store.update_run(run_id, status="executing", summary_json={"total": n, "passed": n if status == "passed" else 0,
            "failed": n if status == "failed" else 0, "error": n if status == "error" else 0, "blocked": 0})
        write_report(agent, run_id)
        agent.store.update_run(run_id, status="completed")
        return run_id

    complete(template)
    slots = [{"id": "import", "run_id": template, "plan_hash": agent.store.get_run(template)["plan_hash"]}]
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    args = ("PCC", "supplier", "v1", slots, 3600, expiry)
    preview = service.preview(*args, receipts["reviewer"])
    service.publish(*args, preview["definition_hash"], receipts["reviewer"])
    batch = service.open_batch("PCC", "supplier", "v1", preview["definition_hash"], receipts["requester"])["batch_id"]
    yield app, service, agent, receipts, tokens, users, template, batch, args, preview, complete
    agent.close()
    agent.store.db.close()


def child(suites, monkeypatch, *, status="passed"):
    _, service, agent, receipts, _, _, _, batch, _, _, complete = suites
    def spawn(run_id, *args, **kwargs):
        assert agent.store.q("SELECT run_id FROM regression_attempts WHERE run_id=?", (run_id,))
        assert not agent.store.db.in_transaction
        return run_id
    monkeypatch.setattr(agent, "_spawn", spawn)
    run_id = service.start_attempt(batch, "import", receipts["requester"])
    return complete(run_id, status=status)


def gate(suites):
    _, service, _, receipts, _, _, _, batch, *_ = suites
    return service.gate(batch, receipts["requester"])


def test_sealed_complete_suite_passes_but_authorizes_no_release(suites, monkeypatch):
    run_id = child(suites, monkeypatch)
    _, service, _, receipts, _, _, _, batch, *_ = suites
    assert gate(suites)["verdict"] == "UNKNOWN"
    service.seal(batch, receipts["requester"])
    b, d, _ = service.batch(batch, receipts["requester"])
    assert service.attempt_verdict(run_id, b, d, d["slots"][0], {"bytes": 0, "deadline": time.monotonic() + 10})["verdict"] == "PASS"
    result = gate(suites)
    assert result["verdict"] == "PASS" and result["release_authorized"] is False
    assert result["attempts"][0]["run_id"] == run_id and not result["missing_slots"]
    digest = result.pop("gate_hash")
    assert digest == sha256(canon(result))


def test_failed_attempt_is_retained_after_passing_rerun(suites, monkeypatch):
    first = child(suites, monkeypatch, status="failed")
    second = child(suites, monkeypatch)
    _, service, _, receipts, _, _, _, batch, *_ = suites
    service.seal(batch, receipts["requester"])
    result = gate(suites)
    assert result["verdict"] == "FAIL"
    assert [(a["run_id"], a["verdict"]) for a in result["attempts"]] == [(first, "FAIL"), (second, "PASS")]


def test_empty_or_partial_sealed_suite_is_unknown(suites):
    _, service, _, receipts, _, _, _, batch, *_ = suites
    service.seal(batch, receipts["requester"])
    result = gate(suites)
    assert result["verdict"] == "UNKNOWN" and result["missing_slots"] == ["import"]


@pytest.mark.parametrize("status", ["planning", "queued_planning", "awaiting_approval", "executing", "cancelled", "blocked", "interrupted", "failed", "analysis_only"])
def test_incomplete_child_never_passes(suites, monkeypatch, status):
    run_id = child(suites, monkeypatch)
    suites[2].store.update_run(run_id, status=status)
    suites[1].seal(suites[7], suites[3]["requester"])
    assert gate(suites)["verdict"] == "UNKNOWN"


@pytest.mark.parametrize("change", ["report", "checksum", "trace", "missing_log", "empty_screenshot", "oracle", "fixture", "config",
                                    "envelope", "source", "result", "summary", "plan", "model", "cancel", "approval", "expired"])
def test_changed_inputs_or_incomplete_evidence_are_unknown(suites, monkeypatch, change):
    run_id = child(suites, monkeypatch)
    _, service, agent, receipts, _, _, _, batch, *_ = suites
    run = agent.store.get_run(run_id)
    result = agent.store.results(run_id)[0]
    paths = {"report": "report.json", "checksum": "report.json.sha256", "trace": result["trace"], "oracle": "inputs/oracles.json",
             "fixture": "inputs/fixtures/" + next(iter(run["execution_envelope_json"]["fixtures"])), "config": "inputs/config.json",
             "envelope": "inputs/envelope.json", "source": "inputs/source-bundle.json"}
    if change in paths:
        agent.artifacts.write_bytes(run_id, paths[change], b"changed")
    elif change == "missing_log": agent.artifacts.path_for(run_id, result["log"]).unlink()
    elif change == "empty_screenshot": agent.artifacts.write_bytes(run_id, result["screenshot"], b"")
    elif change == "result":
        result["status"] = "error";agent.store.save_result(run_id, result)
    elif change == "summary": agent.store.update_run(run_id, summary_json={"total": 0})
    elif change == "plan": agent.store.update_run(run_id, plan_json={"test_cases": []})
    elif change == "model": agent.store.update_run(run_id, model_id="changed")
    elif change == "cancel": agent.store.update_run(run_id, cancel_requested=1)
    elif change == "approval": agent.approvals.record(run_id, run["plan_hash"], "reviewer", "rejected", identity="reviewer", auth_mode="local")
    elif change == "expired": service.clock = lambda: time.time() + 3601
    if change == "expired":
        with pytest.raises(SuiteDenied, match="expired"): gate(suites)
    else:
        service.seal(batch, receipts["requester"])
        assert gate(suites)["verdict"] == "UNKNOWN"


def test_registration_failure_rolls_back_run_and_allows_no_model(suites, monkeypatch):
    _, service, agent, receipts, _, _, _, batch, *_ = suites
    service.seal(batch, receipts["requester"])
    before = agent.store.q("SELECT id FROM runs")
    monkeypatch.setattr(agent, "_spawn", lambda *a, **k: pytest.fail("planning started before admission"))
    with pytest.raises(SuiteDenied, match="sealed"): service.start_attempt(batch, "import", receipts["requester"])
    assert agent.store.q("SELECT id FROM runs") == before
    assert not agent.store.q("SELECT * FROM regression_attempts")
    assert not agent.store.db.in_transaction


def test_attempt_bound_and_seal_are_durable_across_connections(suites, monkeypatch):
    for _ in range(3): child(suites, monkeypatch)
    _, service, agent, receipts, _, _, _, batch, *_ = suites
    peer_agent = QAAgent(agent.store.data_dir, agent_cfg=copy.deepcopy(agent.cfg))
    other = peer_agent.store
    peer = RegressionSuites(peer_agent, service.authorization)
    try:
        assert len(other.q("SELECT * FROM regression_attempts WHERE batch_id=?", (batch,))) == 3
        before = len(agent.store.q("SELECT id FROM runs"))
        with pytest.raises(SuiteDenied, match="limit"): service.start_attempt(batch, "import", receipts["requester"])
        assert len(agent.store.q("SELECT id FROM runs")) == before
        peer.seal(batch, receipts["requester"])
        with pytest.raises(SuiteDenied, match="sealed"): service.start_attempt(batch, "import", receipts["requester"])
    finally:
        peer_agent.close()
        other.db.close()


@pytest.mark.parametrize("who", ["other", "viewer"])
def test_unrelated_identities_cannot_access_or_add_attempts(suites, who):
    _, service, _, receipts, _, _, _, batch, *_ = suites
    with pytest.raises(SuiteDenied): service.gate(batch, receipts[who])
    with pytest.raises(SuiteDenied): service.start_attempt(batch, "import", receipts[who])


def test_publisher_revocation_disabled_feature_and_restore_fail_closed(suites):
    _, service, agent, receipts, _, users, _, batch, *_ = suites
    users.add("reviewer", "approver")
    with pytest.raises(SuiteDenied, match="authorization"): gate(suites)
    agent.cfg["regression"]["enabled"] = False
    with pytest.raises(SuiteDenied, match="disabled"): gate(suites)
    agent.cfg["regression"]["enabled"] = True
    agent.gateway.record(None, "maintenance.restore", {}, "allow", "synthetic restore boundary")
    with pytest.raises(SuiteDenied, match="restored"): service.start_attempt(batch, "import", receipts["requester"])


def test_expiring_publisher_and_service_identities_cannot_authorize_suites(suites, monkeypatch):
    app, service, agent, receipts, _, users, _, _, args, _, _ = suites
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    reviewer_token = users.add("reviewer", "approver", expires_at=expiry)
    reviewer = {"mode": "local_credential", "principal": vars(users.verify(reviewer_token)),
                "revision": app.state.sessions.revision("reviewer")}
    changed = list(args);changed[2] = "expiring"
    preview = service.preview(*changed, reviewer)
    service.publish(*changed, preview["definition_hash"], reviewer)
    batch = service.open_batch("PCC", "supplier", "expiring", preview["definition_hash"], receipts["requester"])["batch_id"]
    bot_token = users.add("bot", "approver", kind="service", expires_at=expiry)
    bot = {"mode": "local_credential", "principal": vars(users.verify(bot_token)), "revision": app.state.sessions.revision("bot")}
    with pytest.raises(SuiteDenied, match="human"): service.gate(batch, bot)
    monkeypatch.setattr(app.state.sessions, "clock", lambda: time.time() + 120)
    with pytest.raises(SuiteDenied, match="authorization"): service.gate(batch, receipts["requester"])
    assert not agent.store.q("SELECT * FROM regression_attempts")


def test_audit_projection_failure_retains_attempt_without_model_cache_or_planning(suites, monkeypatch):
    _, service, agent, receipts, _, _, _, batch, *_ = suites
    before = set(agent._run_models)
    original = agent.gateway.audit_delivery.flush
    def fail_after_commit(*args, **kwargs):
        if agent.store.db.in_transaction:
            return original(*args, **kwargs)  # Production flush defers projection until commit.
        raise RuntimeError("synthetic projection outage")
    monkeypatch.setattr(agent.gateway.audit_delivery, "flush", fail_after_commit)
    monkeypatch.setattr(agent, "_spawn", lambda *a, **k: pytest.fail("planning started after projection failure"))
    with pytest.raises(RuntimeError, match="projection outage"): service.start_attempt(batch, "import", receipts["requester"])
    assert set(agent._run_models) == before
    attempts = agent.store.q("SELECT run_id FROM regression_attempts WHERE batch_id=?", (batch,))
    assert len(attempts) == 1
    assert agent.store.get_run(attempts[0]["run_id"])["status"] == "planning"


def test_versions_and_attempts_are_immutable_and_hash_review_is_required(suites, monkeypatch):
    _, service, agent, receipts, _, _, _, _, args, preview, _ = suites
    with pytest.raises(SuiteDenied, match="changed"): service.publish(*args, "0" * 64, receipts["reviewer"])
    with pytest.raises(SuiteDenied, match="already exists"): service.publish(*args, preview["definition_hash"], receipts["reviewer"])
    child(suites, monkeypatch)
    service.seal(suites[7], receipts["requester"])
    for table in ("regression_versions", "regression_batches", "regression_attempts", "regression_seals"):
        with pytest.raises(sqlite3.IntegrityError): agent.store.x("DELETE FROM " + table)
        with pytest.raises(sqlite3.IntegrityError):
            column = "digest" if table == "regression_versions" else ("owner" if table == "regression_batches" else "created")
            agent.store.x("UPDATE " + table + " SET " + column + "='edited'")
    assert agent.store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3


def test_contract_binds_every_input_except_requester(suites):
    run = suites[2].store.get_run(suites[6])
    original = contract(run)
    for name in ("oracle_sha256", "config_sha256", "policy_sha256", "catalog_version", "environment", "dependencies", "model"):
        changed = copy.deepcopy(run)
        changed["execution_envelope_json"][name] = "changed"
        with pytest.raises(SuiteDenied): contract(changed)
    changed = copy.deepcopy(run)
    changed["requested_by"] = "different-human"
    changed["execution_envelope_json"]["scope"]["requested_by"] = "different-human"
    changed["plan_hash"] = sha256(canon(changed["execution_envelope_json"]))
    assert contract(changed) == original


def test_real_planning_handoff_still_requires_separate_human_review(suites, planned_seed, monkeypatch):
    _, service, agent, receipts, tokens, users, _, batch, *_ = suites
    run_id = service.start_attempt(batch, "import", receipts["requester"], wait=True)
    run = agent.store.get_run(run_id)
    assert run["status"] == "awaiting_approval", run.get("error")
    assert contract(run) == planned_seed[3]  # Same untouched replay inputs produce a comparable contract.
    assert not agent.approvals.latest(run_id)
    assert not agent.store.results(run_id)
    assert gate(suites)["verdict"] == "UNKNOWN"
    monkeypatch.setattr(agent.tracker, "get_issue", lambda *a: pytest.fail("changed suite reached a source read"))
    with pytest.raises(ValueError, match="suite execution inputs changed"):
        agent.review_plan(run_id, users.verify(tokens["reviewer"]), "approved", run["plan_hash"])
    assert not agent.approvals.latest(run_id)


@pytest.mark.parametrize("change", ["publisher", "expiry", "disabled", "missing_authorizer"])
def test_suite_authority_is_rechecked_before_source_reads_and_dispatch(suites, monkeypatch, change):
    run_id = child(suites, monkeypatch)
    _, service, agent, _, _, users, *_ = suites
    run = agent.store.get_run(run_id)
    verify_bound_run(agent, run)
    monkeypatch.setattr(agent.tracker, "get_issue", lambda *a: pytest.fail("revoked suite reached a source read"))
    if change == "publisher": users.disable("reviewer")
    elif change == "expiry": service.clock = lambda: time.time() + 3601
    elif change == "disabled": agent.cfg["regression"]["enabled"] = False
    else: delattr(agent, "regression")
    with pytest.raises(ValueError, match="(authorization|expired|disabled|unavailable)"):
        agent._verify_package(run)


@pytest.mark.parametrize("change", ["publisher", "expiry"])
def test_suite_authority_changed_during_source_refresh_is_rejected(suites, monkeypatch, change):
    run_id = child(suites, monkeypatch)
    _, service, agent, _, _, users, *_ = suites
    original, observed = agent.tracker.get_issue, []
    def refresh(key):
        value = original(key)
        observed.append(key)
        if change == "publisher": users.disable("reviewer")
        else: service.clock = lambda: time.time() + 3601
        return value
    monkeypatch.setattr(agent.tracker, "get_issue", refresh)
    with pytest.raises(ValueError, match="(authorization|expired)"):
        agent._verify_package(agent.store.get_run(run_id))
    assert len(observed) == 1


def test_http_scope_validation_and_preview_publish_protocol(suites):
    app, _, _, _, tokens, _, template, batch, args, _, _ = suites
    headers = lambda name: {"Authorization": "Bearer " + tokens[name]}
    body = {"slots": args[3], "max_age_s": args[4], "expires_at": args[5]}
    root = "/api/projects/PCC/regression-suites/new/versions/v1"
    with TestClient(app) as client:
        assert client.post(root + "/preview", json=body).status_code == 401
        assert client.post(root + "/preview", json=body, headers=headers("requester")).status_code == 403
        preview = client.post(root + "/preview", json=body, headers=headers("reviewer"))
        assert preview.status_code == 200, preview.text
        assert client.post(root, json=body, headers=headers("reviewer")).status_code == 409
        body["definition_hash"] = preview.json()["definition_hash"]
        assert client.post(root, json=body, headers=headers("reviewer")).status_code == 200
        assert client.post(root, json={**body, "max_age_s": True}, headers=headers("reviewer")).status_code == 422
        assert client.post(f"/api/regression-batches/{batch}/gate", headers=headers("other")).status_code == 409
        result = client.post(f"/api/regression-batches/{batch}/gate", headers=headers("requester"))
        assert result.status_code == 200 and result.json()["verdict"] == "UNKNOWN"


def test_queued_admission_binds_attempt_and_job_before_worker(suites, monkeypatch):
    _, service, agent, receipts, _, _, _, batch, *_ = suites
    agent.queue = QueuedRuntime(agent)
    observed = []
    def wake():
        rows = agent.store.q("SELECT a.run_id,j.state FROM regression_attempts a JOIN jobs j ON j.run_id=a.run_id WHERE a.batch_id=?", (batch,))
        assert len(rows) == 1 and rows[0]["state"] == "queued"
        assert not agent.store.db.in_transaction
        observed.extend(rows)
    monkeypatch.setattr(agent.queue.workers, "wake", wake)
    run_id = service.start_attempt(batch, "import", receipts["requester"])
    assert observed[0]["run_id"] == run_id
    assert agent.store.get_run(run_id)["status"] == "queued_planning"
    assert not agent.approvals.latest(run_id)
    service.seal(batch, receipts["requester"])
    before = agent.store.q("SELECT id FROM jobs")
    with pytest.raises(SuiteDenied, match="sealed"): service.start_attempt(batch, "import", receipts["requester"])
    assert agent.store.q("SELECT id FROM jobs") == before


def test_report_age_cannot_be_refreshed_by_run_metadata(suites, monkeypatch):
    run_id = child(suites, monkeypatch)
    agent, service = suites[2], suites[1]
    body = json.loads(agent.artifacts.path_for(run_id, "report.json").read_bytes())
    body["generated_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    raw = json.dumps(body).encode()
    agent.artifacts.write_bytes(run_id, "report.json", raw)
    agent.artifacts.write_bytes(run_id, "report.json.sha256", (sha256(raw) + "  report.json\n").encode())
    agent.store.update_run(run_id, status="completed")
    service.seal(suites[7], suites[3]["requester"])
    result = gate(suites)
    assert result["verdict"] == "UNKNOWN" and result["attempts"][0]["reason"] == "execution report expired"


def test_comparable_passing_results_with_a_declared_gap_are_unknown(suites, monkeypatch):
    _, service, agent, receipts, _, _, template, _, args, _, complete = suites
    run = agent.store.get_run(template)
    planning = copy.deepcopy(run["planning_json"])
    planning["coverage_gaps"] = [{"kind": "out_of_scope", "ac_refs": [], "reason": "Constructed unresolved suite scope"}]
    agent.store.update_run(template, planning_json=planning)
    digest = EP.capture(agent, template, agent.envs.resolve(run["environment"]))
    agent.approvals.record(template, digest, "reviewer", "approved", identity="reviewer", auth_mode="local")
    complete(template)
    slots = [{"id": "import", "run_id": template, "plan_hash": digest}]
    preview = service.preview("PCC", "supplier", "with-gap", slots, args[4], args[5], receipts["reviewer"])
    service.publish("PCC", "supplier", "with-gap", slots, args[4], args[5], preview["definition_hash"], receipts["reviewer"])
    batch = service.open_batch("PCC", "supplier", "with-gap", preview["definition_hash"], receipts["requester"])["batch_id"]
    changed = list(suites);changed[7] = batch
    child(changed, monkeypatch)
    service.seal(batch, receipts["requester"])
    result = service.gate(batch, receipts["requester"])
    assert result["verdict"] == "UNKNOWN" and result["attempts"][0]["reason"] == "suite has uncovered acceptance criteria"


def test_evidence_deadline_and_byte_exhaustion_never_pass(suites, monkeypatch):
    run_id = child(suites, monkeypatch)
    service = suites[1]
    b, d, _ = service.batch(suites[7], suites[3]["requester"])
    for budget in ({"bytes": 0, "deadline": 0}, {"bytes": 100000000, "deadline": time.monotonic() + 10}):
        with pytest.raises(SuiteDenied, match="(deadline|byte)"):
            service.attempt_verdict(run_id, b, d, d["slots"][0], budget)
    service.seal(suites[7], suites[3]["requester"])
    monkeypatch.setattr(service, "read", lambda *a: (_ for _ in ()).throw(SuiteDenied("gate evidence deadline exceeded")))
    assert gate(suites)["verdict"] == "UNKNOWN"


def test_audit_failure_rolls_back_admission_and_no_model_starts(suites, monkeypatch):
    _, service, agent, receipts, _, _, _, batch, *_ = suites
    before = agent.store.q("SELECT id FROM runs")
    monkeypatch.setattr(service, "audit", lambda *a: (_ for _ in ()).throw(RuntimeError("synthetic audit outage")))
    monkeypatch.setattr(agent, "_spawn", lambda *a, **k: pytest.fail("unrecorded run reached planning"))
    with pytest.raises(RuntimeError, match="audit outage"): service.start_attempt(batch, "import", receipts["requester"])
    assert agent.store.q("SELECT id FROM runs") == before
    assert not agent.store.q("SELECT * FROM regression_attempts")


@pytest.mark.parametrize("change", ["passing_label", "expected", "oracle_ref", "ac_refs", "diffs"])
def test_self_consistent_report_cannot_override_oracle_comparison(suites, monkeypatch, change):
    run_id = child(suites, monkeypatch)
    service, agent = suites[1], suites[2]
    result = agent.store.results(run_id)[0]
    if change == "passing_label": result["actual"] = {}
    elif change == "expected": result["expected"] = {}
    elif change == "oracle_ref": result["oracle_ref"] = "changed"
    elif change == "ac_refs": result["ac_refs"] = []
    elif change == "diffs": result["diffs"] = [{"field": "constructed-inconsistent-difference"}]
    agent.store.save_result(run_id, result)
    agent.store.update_run(run_id, status="executing")
    write_report(agent, run_id)
    agent.store.update_run(run_id, status="completed")
    service.seal(suites[7], suites[3]["requester"])
    assert gate(suites)["verdict"] == "UNKNOWN"


def test_backup_restore_preserves_suite_history_but_invalidates_use(suites, tmp_path):
    app, service, agent, receipts, _, users, _, batch, *_ = suites
    archive, key = tmp_path / "suite.qab", os.urandom(32)
    backup(agent.store.data_dir, archive, key, "synthetic-suite-ledger", confirmed_quiescent=True)
    restore(archive, tmp_path / "restored", key, "synthetic-suite-ledger")
    restored = create_app(tmp_path / "restored", auth_mode="local", users_file=users.path, agent_cfg=copy.deepcopy(agent.cfg))
    try:
        assert restored.state.agent.store.q("SELECT * FROM regression_batches") == agent.store.q("SELECT * FROM regression_batches")
        assert restored.state.agent.store.q("SELECT * FROM regression_versions") == agent.store.q("SELECT * FROM regression_versions")
        with pytest.raises(SuiteDenied, match="restored"): restored.state.regression.gate(batch, receipts["requester"])
    finally:
        restored.state.agent.close();restored.state.agent.store.db.close()


def test_schema_v2_migrates_additively_and_preserves_history(tmp_path):
    store = Store(tmp_path)
    store.create_run("legacy", "synthetic prior schema", "synthetic-local", "replay", "Replay")
    for table in ("regression_versions", "regression_batches", "regression_attempts", "regression_seals"):
        store.x("DROP TABLE " + table)
    store.x("PRAGMA user_version=2")
    before = store.get_run("legacy")
    store.db.close()
    upgraded = Store(tmp_path)
    try:
        assert upgraded.get_run("legacy")["request"] == before["request"]
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert upgraded.q("SELECT * FROM regression_versions") == []
    finally:
        upgraded.db.close()


@pytest.mark.integration
def test_real_synthetic_browser_failures_reach_suite_gate(tmp_path, monkeypatch, sut_factory):
    with sut_factory("all") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        users = UserStore(tmp_path / "users.json")
        tokens = {name: users.add(name, role) for name, role in (("owner", "requester"), ("reviewer", "approver"))}
        app = create_app(tmp_path / "state", auth_mode="local", users_file=users.path,
                         agent_cfg=agent_cfg("replay", regression={"enabled": True}))
        agent, service = app.state.agent, app.state.regression
        receipts = {name: {"mode": "local_credential", "principal": vars(users.verify(token)),
                           "revision": app.state.sessions.revision(name)} for name, token in tokens.items()}
        try:
            template = agent.start_run("PCC-DEMO-1", story_key="PCC-DEMO-1", project="PCC", requested_by="owner", wait=True)
            reviewed = agent.store.get_run(template)
            agent.review_plan(template, users.verify(tokens["reviewer"]), "approved", reviewed["plan_hash"])
            agent.execute_approved(template, wait=True)
            assert agent.store.get_run(template)["status"] == "completed"
            assert any(r["status"] == "failed" for r in agent.store.results(template))
            slots = [{"id": "import", "run_id": template, "plan_hash": reviewed["plan_hash"]}]
            expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            args = ("PCC", "browser", "v1", slots, 3600, expiry)
            preview = service.preview(*args, receipts["reviewer"])
            service.publish(*args, preview["definition_hash"], receipts["reviewer"])
            batch = service.open_batch("PCC", "browser", "v1", preview["definition_hash"], receipts["owner"])["batch_id"]
            child_run = service.start_attempt(batch, "import", receipts["owner"], wait=True)
            run = agent.store.get_run(child_run)
            assert run["status"] == "awaiting_approval" and not agent.approvals.latest(child_run)
            assert contract(run) == preview["definition"]["slots"][0]["contract"]
            agent.review_plan(child_run, users.verify(tokens["reviewer"]), "approved", run["plan_hash"])
            agent.execute_approved(child_run, wait=True)
            service.seal(batch, receipts["owner"])
            result = service.gate(batch, receipts["owner"])
            assert result["verdict"] == "FAIL", result
            assert result["attempts"][0]["report_hash"]
            assert result["release_authorized"] is False
        finally:
            agent.close();agent.store.db.close()
