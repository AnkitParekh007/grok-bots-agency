"""Evidence quotas/publication, independent SQLite writers and fail-closed capture."""
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from qa_agent.storage import FileArtifactStore, StorageLimitExceeded
from qa_agent.store import Store
from qa_agent.runtime import QAAgent
from qa_agent.server import create_app
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.security, pytest.mark.audit, pytest.mark.execution]


def small(**over):
    return {"max_file_bytes": 8, "max_run_bytes": 12, "max_total_bytes": 20,
            "max_run_files": 3, "max_total_files": 4, "minimum_free_bytes": 1_000_000,
            "inventory_timeout_s": 5, **over}


@pytest.mark.parametrize("limits", [[], False, {"enabled": False}, {"max_file_bytes": True}, {"max_file_bytes": 0},
                                     {"max_file_bytes": 64_000_001}, {"max_run_bytes": 8}, {"max_total_files": 0},
                                     {"minimum_free_bytes": 999}, {"inventory_timeout_s": 31}])
def test_invalid_or_disableable_limits_are_refused(tmp_path, limits):
    with pytest.raises(ValueError): FileArtifactStore(tmp_path / "runs", limits=limits)


def test_byte_boundaries_replacement_and_combined_run_total_limits(tmp_path):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small())
    artifacts.write_bytes("a", "one", b"12345678")
    with pytest.raises(StorageLimitExceeded): artifacts.write_bytes("a", "too-big", b"x" * 9)
    artifacts.write_bytes("a", "two", b"1234")
    with pytest.raises(StorageLimitExceeded): artifacts.write_bytes("a", "three", b"x")
    artifacts.write_bytes("b", "one", b"12345678")
    with pytest.raises(StorageLimitExceeded): artifacts.ensure_capacity()
    with pytest.raises(StorageLimitExceeded): artifacts.write_bytes("c", "one", b"x")
    artifacts.write_bytes("a", "one", b"x")  # Charge replacement delta, retaining atomic old/new disk reserve.
    assert artifacts.ensure_capacity()["artifact_bytes"] == 13
    assert len(artifacts.manifest("a")) == 2


def test_file_counts_include_zero_length_files(tmp_path):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small(max_run_files=1, max_total_files=2))
    artifacts.write_bytes("a", "one", b"")
    with pytest.raises(StorageLimitExceeded): artifacts.write_bytes("a", "two", b"")
    artifacts.write_bytes("b", "one", b"")
    with pytest.raises(StorageLimitExceeded): artifacts.write_bytes("c", "one", b"")
    assert not (artifacts.root / "c").exists()


def test_execution_preflight_counts_required_files_without_reserving_or_creating_them(tmp_path):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small(max_run_files=6, max_total_files=6))
    plan = {"test_cases": [{"id": "TC-1"}]}
    result = artifacts.check_execution_capacity("a", plan)
    assert result["artifact_files"] == 6 and not list(artifacts.root.iterdir())
    artifacts.write_bytes("a", "evidence/TC-1/trace.zip", b"trace")
    assert artifacts.check_execution_capacity("a", plan)["artifact_files"] == 6
    with pytest.raises(StorageLimitExceeded):
        artifacts.check_execution_capacity("a", {"test_cases": [{"id": "TC-1"}, {"id": "TC-2"}]})


def test_impossible_file_budget_cannot_be_human_approved(tmp_path):
    from qa_agent.auth import Principal
    agent = QAAgent(tmp_path, agent_cfg=agent_cfg("replay"))
    try:
        rid = agent.start_run("PCC-DEMO-1", requested_by="requester", wait=True)
        run = agent.store.get_run(rid)
        assert run["status"] == "awaiting_approval", run["error"]
        agent.artifacts.limits["max_run_files"] = len(agent.artifacts.manifest(rid)) + 5
        with pytest.raises(StorageLimitExceeded):
            agent.review_plan(rid, Principal("reviewer", "approver", "local"), "approved", run["plan_hash"])
        assert agent.approvals.latest(rid) is None
    finally:
        agent.close(); agent.store.db.close()


@pytest.mark.parametrize("failure", ["fsync", "replace", "lease"])
def test_failed_atomic_publication_preserves_previous_bytes(tmp_path, failure):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small())
    artifacts.write_bytes("a", "one", b"old")
    def lost(*args, **kwargs): raise RuntimeError("synthetic lease lost")
    if failure == "lease":
        original = artifacts._guard
        calls = [0]
        def guard(*args, **kwargs):
            calls[0] += 1
            if calls[0] >= 3: lost()
            return original(*args, **kwargs)
        context = patch.object(artifacts, "_guard", side_effect=guard)
    else:
        context = patch("qa_agent.storage.os." + failure, side_effect=OSError("synthetic I/O error"))
    with context, pytest.raises((OSError, RuntimeError)):
        artifacts.write_bytes("a", "one", b"new")
    assert (artifacts.root / "a/one").read_bytes() == b"old"
    assert sorted(p.name for p in (artifacts.root / "a").iterdir()) == ["one"]


def test_disk_reserve_includes_full_atomic_replacement_not_only_delta(tmp_path):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small())
    artifacts.write_bytes("a", "one", b"old")
    usage = SimpleNamespace(free=1_000_002)
    with patch("qa_agent.storage.shutil.disk_usage", return_value=usage), pytest.raises(StorageLimitExceeded):
        artifacts.write_bytes("a", "one", b"new")
    assert artifacts.path_for("a", "one").read_bytes() == b"old"


def test_linked_temporary_file_cannot_be_published(tmp_path):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small())
    artifacts.write_bytes("a", "one", b"old")
    original = os.fsync
    alias = tmp_path / "outside-alias"
    def linked(fd):
        original(fd)
        partial = next((artifacts.root / "a").glob(".artifact-*"))
        os.link(partial, alias)
    with patch("qa_agent.storage.os.fsync", side_effect=linked), pytest.raises(StorageLimitExceeded):
        artifacts.write_bytes("a", "one", b"new")
    assert artifacts.path_for("a", "one").read_bytes() == b"old"
    assert alias.read_bytes() == b"new"  # Never remove an out-of-band file during cleanup.


def test_generated_capture_is_private_bounded_and_cleans_only_its_own_file(tmp_path):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small())
    artifacts.stage.mkdir()
    preserved = artifacts.stage / "other.preserved.part"
    preserved.write_bytes(b"keep")
    def oversized(path): path.write_bytes(b"x" * 9)
    with pytest.raises(StorageLimitExceeded): artifacts.capture_file("a", "trace.zip", oversized)
    assert not (artifacts.root / "a/trace.zip").exists()
    assert list(artifacts.stage.iterdir()) == [preserved]
    artifacts.capture_file("a", "trace.zip", lambda path: path.write_bytes(b"trace"))
    assert artifacts.path_for("a", "trace.zip").read_bytes() == b"trace"
    assert list(artifacts.stage.iterdir()) == [preserved]
    with pytest.raises(StorageLimitExceeded): artifacts.capture_file("b", "empty", lambda path: None)
    assert list(artifacts.stage.iterdir()) == [preserved]


def test_staging_exception_and_lost_authority_do_not_publish(tmp_path):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small())
    def crash(path):
        path.write_bytes(b"partial")
        raise RuntimeError("synthetic producer failure")
    with pytest.raises(RuntimeError): artifacts.capture_file("a", "trace.zip", crash)
    def lose(path):
        path.write_bytes(b"trace")
        artifacts.guard = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("lease lost"))
    with pytest.raises(RuntimeError): artifacts.capture_file("a", "trace.zip", lose)
    assert not list(artifacts.stage.iterdir())
    assert not (artifacts.root / "a/trace.zip").exists()


def test_hardlinks_paths_and_private_staging_are_refused(tmp_path):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small())
    artifacts.write_bytes("a", "one", b"safe")
    outside = tmp_path / "outside"
    os.link(artifacts.path_for("a", "one"), outside)
    with pytest.raises(StorageLimitExceeded): artifacts.ensure_capacity()
    with pytest.raises(StorageLimitExceeded): artifacts.path_for("a", "one", create=False)
    assert outside.read_bytes() == b"safe"
    for rel in ("../escape", ".artifact-hidden", "nested/../escape"):
        with pytest.raises(ValueError): artifacts.path_for("a", rel)


def test_empty_directory_inventory_is_bounded(tmp_path):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small(max_total_files=4))
    for index in range(150): (artifacts.root / str(index)).mkdir()
    with pytest.raises(StorageLimitExceeded, match="traversal"):
        artifacts.ensure_capacity()


def test_inventory_deadline_and_existing_oversized_evidence_are_refused(tmp_path):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small())
    artifacts.write_bytes("a", "one", b"12345678")
    with patch("qa_agent.storage.time.monotonic", side_effect=[0, 6]), pytest.raises(StorageLimitExceeded, match="traversal"):
        artifacts.ensure_capacity()
    artifacts.path_for("a", "one").write_bytes(b"oversized")
    with pytest.raises(StorageLimitExceeded): artifacts.ensure_capacity()
    with pytest.raises(StorageLimitExceeded): artifacts.manifest("a")


def test_empty_inventory_still_checks_its_deadline(tmp_path):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small())
    with patch("qa_agent.storage.time.monotonic", side_effect=[0, 6]), pytest.raises(StorageLimitExceeded):
        artifacts.ensure_capacity()


def test_manifest_uses_bounded_streams_and_excludes_native_staging(tmp_path, monkeypatch):
    artifacts = FileArtifactStore(tmp_path / "runs", limits=small())
    artifacts.write_bytes("a", "one", b"12345678")
    artifacts.stage.mkdir()
    (artifacts.stage / "a.abandoned.part").write_bytes(b"old")
    monkeypatch.setattr(Path, "read_bytes", lambda *args: (_ for _ in ()).throw(AssertionError("unbounded read")))
    manifest = artifacts.manifest("a")
    assert [row["path"] for row in manifest] == ["one"] and manifest[0]["bytes"] == 8


def test_independent_database_writers_cannot_both_spend_the_same_capacity(tmp_path):
    first, second = Store(tmp_path), Store(tmp_path)
    barrier = threading.Barrier(2)
    limits = small(max_file_bytes=8, max_run_bytes=8, max_total_bytes=8)
    writers = [FileArtifactStore(tmp_path / "runs", store=store, limits=limits) for store in (first, second)]
    def write(pair):
        index, artifact = pair
        barrier.wait(timeout=5)
        try: artifact.write_bytes("run-" + str(index), "evidence", b"12345678"); return "published"
        except StorageLimitExceeded: return "denied"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, enumerate(writers)))
        assert sorted(results) == ["denied", "published"]
        assert sum(path.stat().st_size for path in (tmp_path / "runs").rglob("*") if path.is_file()) == 8
    finally:
        first.db.close(); second.db.close()


def test_storage_exhaustion_degrades_readiness_and_blocks_http_and_direct_planning(tmp_path):
    app = create_app(tmp_path, agent_cfg=agent_cfg("replay"))
    agent = app.state.agent
    try:
        agent.artifacts.limits.update(max_total_files=1, max_run_files=1)
        agent.artifacts.write_bytes("prior", "evidence", b"saved")
        with TestClient(app) as client, patch.object(agent.model, "generate") as generate:
            assert client.get("/healthz").status_code == 200
            assert client.get("/readyz").status_code == 503
            assert client.get("/api/environments").status_code == 503
            assert client.post("/api/chat", json={"message": "PCC-DEMO-1"}).status_code == 503
            with pytest.raises(StorageLimitExceeded): agent.start_run("PCC-DEMO-1")
            generate.assert_not_called()
            assert agent.store.list_runs() == []
    finally:
        agent.close(); agent.store.db.close()


def test_tool_dispatch_rechecks_capacity_after_durable_intent(tmp_path, monkeypatch):
    agent = QAAgent(tmp_path, agent_cfg=agent_cfg("replay"))
    original = agent.gateway._audit
    def full(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1] == "tool.intent":
            agent.artifacts.limits.update(max_total_files=1, max_run_files=1)
            agent.artifacts.write_bytes("prior", "evidence", b"saved")
        return result
    monkeypatch.setattr(agent.gateway, "_audit", full)
    try:
        with patch.object(agent.tracker, "get_issue") as read, pytest.raises(StorageLimitExceeded):
            agent._tool(None, "tracker.get_issue", {"key": "PCC-DEMO-1"}, lambda: read("PCC-DEMO-1"))
        read.assert_not_called()
    finally:
        agent.close(); agent.store.db.close()


@pytest.mark.integration
@pytest.mark.parametrize("missing", ["screenshot", "trace", "log"])
def test_required_evidence_failure_is_error_and_stops_following_cases(tmp_path, monkeypatch, sut_factory, missing):
    from qa_agent.storage import StorageLimitExceeded
    with sut_factory("") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        agent = QAAgent(tmp_path, agent_cfg=agent_cfg("replay"))
        try:
            rid = agent.start_run("PCC-DEMO-1", wait=True)
            assert agent.store.get_run(rid)["status"] == "awaiting_approval"
            original_write, original_capture = agent.artifacts.write_bytes, agent.artifacts.capture_file
            def write(run, rel, data):
                if (missing == "screenshot" and rel.endswith("screenshot.png")) or (missing == "log" and rel.endswith("log.txt")):
                    raise StorageLimitExceeded("synthetic evidence failure")
                return original_write(run, rel, data)
            def capture(*args):
                if missing == "trace": raise StorageLimitExceeded("synthetic trace failure")
                return original_capture(*args)
            monkeypatch.setattr(agent.artifacts, "write_bytes", write)
            monkeypatch.setattr(agent.artifacts, "capture_file", capture)
            agent.approve(rid, "synthetic-contract-reviewer", "approved", wait=True)
            results = agent.store.results(rid)
            assert results[0]["status"] == "error" and results[0]["comparison_status"] == "passed"
            assert results[0]["evidence_complete"] is False and missing in results[0]["error"]
            assert len(results) == 8 and all(row["status"] == "not_run" for row in results[1:])
            assert agent.store.get_run(rid)["summary_json"]["passed"] == 0
            report = json.loads(agent.artifacts.path_for(rid, "report.json").read_text())
            assert report["not_run"] == 7
        finally:
            agent.close(); agent.store.db.close()
