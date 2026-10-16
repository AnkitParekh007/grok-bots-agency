"""Audit intent/outbox durability, crash retry and concurrent connection behavior."""
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from qa_agent.runtime import QAAgent
from qa_agent.store import Store
from qa_agent.gateway import PolicyDenied
from qa_agent import audit_delivery
from qa_agent.audit_checkpoint import checkpoint, verify, repair_projections
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

pytestmark = [pytest.mark.audit, pytest.mark.security]


def record(index):
    return {"ts": "2026-10-16T00:00:00+00:00", "run_id": None, "tool": "test", "args_hash": str(index),
            "args_summary": "{}", "decision": "allow", "reason": "synthetic test", "outcome": "ok", "duration_ms": 0}


def test_sequence_and_outbox_allocation_are_atomic_across_store_connections(tmp_path):
    stores = [Store(tmp_path) for _ in range(4)]
    try:
        def append(worker):
            for i in range(15):
                stores[worker].append_audit(record(worker * 15 + i))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append, range(4)))
        rows = stores[0].q("SELECT * FROM audit ORDER BY id")
        assert [r["seq"] for r in rows] == list(range(1, 61))
        assert stores[0].verify_audit_chain()["ok"]
        assert len(stores[0].q("SELECT * FROM audit_outbox")) == 60
    finally:
        for store in stores: store.db.close()


def test_file_delivery_failure_preserves_intent_and_outcome_then_retries(tmp_path):
    agent = QAAgent(tmp_path)
    try:
        with patch("qa_agent.audit_delivery._append", side_effect=OSError("synthetic disk failure")):
            assert agent.gateway.invoke("run-audit", "env.resolve", {}, lambda: "result") == "result"
        rows = agent.store.audit_rows("run-audit")
        assert [r["tool"] for r in rows] == ["tool.intent", "env.resolve"]
        assert agent.gateway.audit_delivery.health()["pending"] == 2
        assert agent.store.verify_audit_chain()["ok"]
        assert agent.gateway.audit_delivery.flush()["pending"] == 0
        assert len(agent.gateway.audit_jsonl.read_text().splitlines()) == 2
        assert len(agent.artifacts.path_for("run-audit", "audit.jsonl").read_text().splitlines()) == 2
    finally:
        agent.store.db.close()


def test_crash_after_fsync_before_ack_does_not_duplicate_global_line(tmp_path):
    agent = QAAgent(tmp_path)
    original = audit_delivery._append
    calls = []
    def interrupted(path, row, **kwargs):
        calls.append(path)
        if not kwargs.get("global_chain"):
            raise OSError("crash before per-run delivery")
        return original(path, row, **kwargs)
    try:
        with patch("qa_agent.audit_delivery._append", side_effect=interrupted):
            agent.gateway.record("run-crash", "synthetic", {}, "allow", "test")
        assert agent.gateway.audit_delivery.health()["pending"] == 1
        assert len(agent.gateway.audit_jsonl.read_text().splitlines()) == 1
        assert agent.gateway.audit_delivery.flush()["pending"] == 0
        assert len(agent.gateway.audit_jsonl.read_text().splitlines()) == 1
        assert len(agent.artifacts.path_for("run-crash", "audit.jsonl").read_text().splitlines()) == 1
    finally:
        agent.store.db.close()


def test_intent_is_durable_before_tool_runs_and_orphan_intent_means_unknown(tmp_path):
    agent = QAAgent(tmp_path)
    try:
        def tool():
            rows = agent.store.audit_rows("intent-run")
            assert len(rows) == 1 and rows[0]["tool"] == "tool.intent" and rows[0]["outcome"] == "pending"
            raise RuntimeError("synthetic failure")
        with pytest.raises(RuntimeError):
            agent.gateway.invoke("intent-run", "env.resolve", {}, tool)
        rows = agent.store.audit_rows("intent-run")
        invocation = json.loads(rows[0]["args_summary"])["invocation"]
        assert invocation in rows[1]["reason"] and "RuntimeError" in rows[1]["outcome"]
    finally:
        agent.store.db.close()


def test_corrupt_projection_is_not_silently_rewritten_and_backlog_stops_dispatch(tmp_path):
    agent = QAAgent(tmp_path)
    try:
        agent.gateway.audit_jsonl.write_bytes(b"partial unknown bytes")
        agent.gateway.record(None, "synthetic", {}, "allow", "test")
        assert agent.gateway.audit_delivery.health()["pending"] == 1
        assert agent.gateway.audit_jsonl.read_bytes() == b"partial unknown bytes"
        agent.policy["audit_delivery"] = {"max_pending": 1}
        called = []
        with pytest.raises(PolicyDenied, match="backlog"):
            agent.gateway.invoke(None, "env.resolve", {}, lambda: called.append(True))
        assert called == []
    finally:
        agent.store.db.close()


def test_independent_signed_checkpoint_detects_changed_prefix_and_wrong_ledger(tmp_path):
    store = Store(tmp_path)
    try:
        for i in range(4): store.append_audit(record(i))
        rows = store.q("SELECT * FROM audit ORDER BY id")
        key = Ed25519PrivateKey.generate()
        receipt = checkpoint(rows, key, "synthetic-ledger")
        assert verify(receipt, key.public_key(), "synthetic-ledger", rows)
        assert not verify(receipt, key.public_key(), "wrong-ledger", rows)
        assert not verify(receipt, key.public_key(), "synthetic-ledger", rows[:-1])
        changed = [dict(row) for row in rows];changed[0]["reason"] = "tampered"
        assert not verify(receipt, key.public_key(), "synthetic-ledger", changed)
        receipt["payload"]["count"] += 1
        assert not verify(receipt, key.public_key(), "synthetic-ledger", rows)
    finally:
        store.db.close()


def test_explicit_projection_repair_preserves_corrupt_bytes_and_recovers_outbox(tmp_path):
    agent = QAAgent(tmp_path)
    try:
        agent.gateway.audit_jsonl.write_bytes(b"partial corrupted projection")
        agent.gateway.record("run-repair", "synthetic", {}, "allow", "test")
        receipt = repair_projections(tmp_path)
        assert receipt["ledger"]["count"] == 1
        assert agent.gateway.audit_delivery.health()["pending"] == 0
        assert json.loads(agent.gateway.audit_jsonl.read_text())["tool"] == "synthetic"
        assert any(path.read_bytes() == b"partial corrupted projection" for path in (tmp_path / "audit-recovery").rglob("*.before.jsonl"))
        agent.gateway.record("run-repair", "next", {}, "allow", "test")
        assert len(agent.gateway.audit_jsonl.read_text().splitlines()) == 2
    finally:
        agent.store.db.close()


def test_projection_repair_refuses_active_execution_without_marking_it_interrupted(tmp_path):
    agent = QAAgent(tmp_path)
    try:
        agent.store.create_run("active", "synthetic", "synthetic-local", "replay", "test")
        agent.store.update_run("active", status="executing")
        with pytest.raises(ValueError, match="quiescent"):
            repair_projections(tmp_path)
        assert agent.store.get_run("active")["status"] == "executing"
    finally:
        agent.store.db.close()


@pytest.mark.parametrize("run_id,relative", [("run-safe", "../run-safe-sibling/secret.json"), ("../outside", "x"), ("run-safe", "/outside")])
def test_artifact_paths_use_component_containment_including_prefix_siblings(tmp_path, run_id, relative):
    from qa_agent.store import FileArtifactStore
    artifacts = FileArtifactStore(tmp_path / "runs")
    with pytest.raises(ValueError):
        artifacts.path_for(run_id, relative)
