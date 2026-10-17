"""Encrypted offline restore, hostile archives, authority rollback and migrations."""
import io
import json
import os
import sqlite3
import struct
from dataclasses import asdict
from unittest.mock import patch

import pytest

from qa_agent.auth import UserStore
from qa_agent.jobs import DurableJobs
from qa_agent.recovery import backup, restore, RecoveryError, _Writer, _Reader, MAX_BYTES, MAX_MANIFEST
from qa_agent.runtime import QAAgent
from qa_agent.sessions import SessionManager
from qa_agent.store import Store, SCHEMA_VERSION, verify_chain
from qa_agent.util import canon, sha256
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.security, pytest.mark.audit, pytest.mark.execution]


@pytest.fixture
def state(tmp_path):
    a = QAAgent(tmp_path / "state", agent_cfg=agent_cfg("replay"))
    users = UserStore(tmp_path / "outside-secrets.json")
    token = users.add("owner", "approver")
    sessions = SessionManager(a.store, users)
    issued = sessions.issue(asdict(users.verify(token)))
    a.store.create_run("saved", "Synthetic recovery fixture", "synthetic-local", "replay", "REPLAYED")
    a.store.update_run("saved", status="awaiting_approval", plan_hash="a" * 64)
    a.approvals.record("saved", "a" * 64, "reviewer", "approved", identity="reviewer", auth_mode="local")
    a.artifacts.write_bytes("saved", "evidence/synthetic-marker.bin", b"SYNTHETIC-PRIVATE-BACKUP-MARKER" + b"x" * 1_200_000)
    a.gateway.record("saved", "recovery.fixture", {}, "allow", "synthetic recovery fixture")
    a.store.create_run("queued", "Synthetic queued work", "synthetic-local", "replay", "REPLAYED")
    a.store.update_run("queued", status="queued_planning")
    DurableJobs(a.store).enqueue("queued", "planning", {"authorization": {"mode": "operator"}}, "a" * 64)
    a.store.x("INSERT INTO eval_grants(grant_id,created_by) VALUES('old-grant','owner')")
    a.store.x("CREATE TABLE oidc_flows(state_hash TEXT PRIMARY KEY)")
    a.store.x("INSERT INTO oidc_flows VALUES('old-flow')")
    key = os.urandom(32)
    yield a, key, issued
    a.store.db.close()


def make_backup(tmp_path, state):
    a, key, _ = state
    archive = tmp_path / "backup.qab"
    result = backup(a.store.data_dir, archive, key, "synthetic-ledger", confirmed_quiescent=True)
    return archive, result


def test_encrypted_set_roundtrip_preserves_evidence_and_revokes_rollback_authority(tmp_path, state):
    a, key, issued = state
    archive, created = make_backup(tmp_path, state)
    assert b"SYNTHETIC-PRIVATE-BACKUP-MARKER" not in archive.read_bytes()
    assert issued["token"].encode() not in archive.read_bytes()
    original = a.artifacts.path_for("saved", "evidence/synthetic-marker.bin").read_bytes()
    receipt = restore(archive, tmp_path / "restored", key, "synthetic-ledger")
    assert receipt["ready_for_deployment"] is False
    assert receipt["blocked_jobs"] == receipt["revoked_sessions"] == receipt["revoked_eval_grants"] == 1
    restored = Store(tmp_path / "restored")
    try:
        assert (tmp_path / "restored/runs/saved/evidence/synthetic-marker.bin").read_bytes() == original
        assert restored.get_run("saved")["status"] == restored.get_run("queued")["status"] == "blocked"
        assert not SessionManager(restored).verify(issued["token"])
        assert restored.q("SELECT * FROM oidc_flows") == []
        assert restored.q("SELECT state FROM jobs")[0]["state"] == "blocked"
        assert restored.q("SELECT revoked_by FROM eval_grants")[0]["revoked_by"] == "restore-maintenance"
        assert restored.q("SELECT decision FROM approvals ORDER BY id DESC")[0]["decision"] == "rejected"
        assert verify_chain(restored.q("SELECT * FROM audit ORDER BY id"))["ok"]
        assert restored.q("SELECT * FROM audit WHERE tool='maintenance.restore'")
        assert created["sha256"] == sha256(archive.read_bytes())
    finally:
        restored.db.close()


def test_restore_never_resurrects_jira_write_authority_or_assumes_upstream_state(tmp_path, state):
    a, key, _ = state
    states = ("pending", "approved", "dispatching", "uncertain", "succeeded", "rejected")
    for index, status in enumerate(states):
        a.store.x("INSERT INTO jira_writes(id,run_id,fingerprint,draft,draft_hash,state,requester,requester_receipt,expires) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (str(index), "saved", str(index), "{}", "synthetic-hash", status, "owner", "{}", 9999999999))
    archive, _ = make_backup(tmp_path, state)
    receipt = restore(archive, tmp_path / "restored", key, "synthetic-ledger")
    assert receipt["invalidated_jira_writes"] == len(states)
    restored = Store(tmp_path / "restored")
    try:
        assert {row["state"] for row in restored.q("SELECT state FROM jira_writes")} == {"restored_reconciliation_required"}
        assert verify_chain(restored.q("SELECT * FROM audit ORDER BY id"))["ok"]
    finally:
        restored.db.close()


@pytest.mark.parametrize("mutation", ["wrong-key", "ciphertext", "truncated", "final-record", "appended"])
def test_authentication_and_ending_failures_never_publish_partial_state(tmp_path, state, mutation):
    _, key, _ = state
    archive, _ = make_backup(tmp_path, state)
    data = bytearray(archive.read_bytes())
    if mutation == "wrong-key": key = os.urandom(32)
    elif mutation == "ciphertext": data[len(data) // 2] ^= 1
    elif mutation == "truncated": data = data[:len(data) // 2]
    elif mutation == "final-record": data = data[:-21]
    else: data.extend(b"unapproved trailer")
    archive.write_bytes(data)
    target = tmp_path / "must-not-appear"
    with pytest.raises(RecoveryError):
        restore(archive, target, key, "synthetic-ledger")
    assert not target.exists()
    assert list(tmp_path.glob(".qa-recovery-*")) == []


def test_existing_destination_and_wrong_ledger_are_preserved(tmp_path, state):
    _, key, _ = state
    archive, _ = make_backup(tmp_path, state)
    with pytest.raises(RecoveryError, match="identity"):
        restore(archive, tmp_path / "other", key, "wrong-ledger")
    existing = tmp_path / "existing"
    existing.mkdir();(existing / "keep").write_text("preserve me")
    with pytest.raises(RecoveryError, match="must not exist"):
        restore(archive, existing, key, "synthetic-ledger")
    assert (existing / "keep").read_text() == "preserve me"


def test_backup_requires_quiescence_and_preserves_existing_backup(tmp_path, state):
    a, key, _ = state
    with pytest.raises(RecoveryError, match="stopped-writer"):
        backup(a.store.data_dir, tmp_path / "not-created", key, "synthetic-ledger")
    a.store.update_run("saved", status="executing")
    with pytest.raises(RecoveryError, match="quiescent"):
        backup(a.store.data_dir, tmp_path / "not-created", key, "synthetic-ledger", confirmed_quiescent=True)
    assert not (tmp_path / "not-created").exists()
    a.store.update_run("saved", status="completed")
    archive, _ = make_backup(tmp_path, state)
    before = archive.read_bytes()
    with pytest.raises(RecoveryError):
        backup(a.store.data_dir, archive, key, "synthetic-ledger", confirmed_quiescent=True)
    assert archive.read_bytes() == before


def test_backup_budget_and_disk_failure_publish_no_partial_archive(tmp_path, state):
    a, key, _ = state
    destination = tmp_path / "not-published"
    with pytest.raises(RecoveryError, match="budget"):
        backup(a.store.data_dir, destination, key, "synthetic-ledger", confirmed_quiescent=True, max_bytes=1024)
    with patch("qa_agent.recovery._Writer.write", side_effect=OSError("synthetic disk-full fault")):
        with pytest.raises(OSError):
            backup(a.store.data_dir, destination, key, "synthetic-ledger", confirmed_quiescent=True)
    assert not destination.exists()
    assert not list(tmp_path.glob(".qa-recovery-*"))
    assert not list(a.store.data_dir.glob(".qa-recovery-*"))


@pytest.mark.parametrize("name", ["../outside", "/absolute", "runs/../outside", "runs/CON.txt", "runs/file:stream", "runs/a\\b", "runs/trailing.", "users.json"])
def test_even_authenticated_hostile_member_names_cannot_escape_restoration(tmp_path, name):
    key = os.urandom(32)
    manifest = {"format": "qa-recovery-set/1", "id": "a" * 32, "ledger_id": "synthetic-ledger", "files": [{"name": name, "bytes": 0, "sha256": sha256(b"")}]}
    encoded = canon(manifest).encode()
    archive = tmp_path / "hostile.qab"
    with archive.open("wb") as output:
        writer = _Writer(output, key);writer.write(struct.pack(">I", len(encoded)) + encoded);writer.finish()
    with pytest.raises(RecoveryError):
        restore(archive, tmp_path / "not-published", key, "synthetic-ledger")
    assert not (tmp_path / "not-published").exists()


def test_future_database_version_is_refused_without_modification(tmp_path):
    store = Store(tmp_path)
    store.db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}");store.db.close()
    before = (tmp_path / "qa_agent.db").read_bytes()
    with pytest.raises(ValueError, match="newer"):
        Store(tmp_path)
    assert (tmp_path / "qa_agent.db").read_bytes() == before


def test_schema_upgrade_is_atomic_and_preserves_legacy_rows_on_failure(tmp_path):
    store = Store(tmp_path)
    store.create_run("legacy", "keep", "synthetic-local", "replay", "REPLAYED")
    store.update_run("legacy", status="completed")
    store.db.execute("PRAGMA user_version=0")
    store.db.execute("DROP TABLE job_quarantines");store.db.close()
    original = Store._migrate
    def failed_migration(self):
        original(self)
        self.db.execute("ALTER TABLE runs ADD COLUMN failed_upgrade TEXT")
        raise RuntimeError("synthetic migration failure")
    with patch.object(Store, "_migrate", failed_migration):
        with pytest.raises(RuntimeError): Store(tmp_path)
    with sqlite3.connect(tmp_path / "qa_agent.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "failed_upgrade" not in {r[1] for r in db.execute("PRAGMA table_info(runs)")}
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='job_quarantines'").fetchone()
        assert db.execute("SELECT request FROM runs WHERE id='legacy'").fetchone()[0] == "keep"
    restored = Store(tmp_path)
    assert restored.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    restored.db.close()


def test_hardlinked_artifact_is_refused_without_archiving_outside_content(tmp_path, state):
    a, key, _ = state
    outside = tmp_path / "outside-sensitive-file"
    outside.write_text("SYNTHETIC-OUTSIDE-MARKER")
    linked = a.artifacts.path_for("saved", "evidence/linked.txt")
    os.link(outside, linked)
    with pytest.raises(RecoveryError, match="hard links"):
        backup(a.store.data_dir, tmp_path / "not-published", key, "synthetic-ledger", confirmed_quiescent=True)
    assert outside.read_text() == "SYNTHETIC-OUTSIDE-MARKER"


@pytest.mark.parametrize("mutation", ["hash", "schema", "duplicate", "missing-database"])
def test_authenticated_but_inconsistent_manifest_is_refused(tmp_path, state, mutation):
    _, key, _ = state
    archive, _ = make_backup(tmp_path, state)
    with archive.open("rb") as source:
        reader = _Reader(source, key, MAX_BYTES + MAX_MANIFEST + 4)
        length = struct.unpack(">I", reader.read(4))[0]
        manifest = json.loads(reader.read(length))
        body = bytearray()
        while part := reader.read(1_048_576): body.extend(part)
    if mutation == "hash": manifest["files"][0]["sha256"] = "0" * 64
    elif mutation == "schema": manifest["schema_version"] = SCHEMA_VERSION + 1
    elif mutation == "duplicate": manifest["files"].append({**manifest["files"][0], "name": manifest["files"][0]["name"].upper()})
    else: manifest["files"] = [f for f in manifest["files"] if f["name"] != "qa_agent.db"]
    with archive.open("wb") as output:
        writer = _Writer(output, key)
        encoded = canon(manifest).encode()
        writer.write(struct.pack(">I", len(encoded)) + encoded);writer.write(body);writer.finish()
    with pytest.raises(RecoveryError): restore(archive, tmp_path / "not-published", key, "synthetic-ledger")
    assert not (tmp_path / "not-published").exists()


def test_invalid_audit_chain_is_not_accepted_as_a_verified_backup(tmp_path, state):
    a, key, _ = state
    a.store.x("DROP TRIGGER audit_no_update")
    a.store.x("UPDATE audit SET reason='synthetic unauthorized modification' WHERE id=1")
    with pytest.raises(ValueError, match="chain"):
        backup(a.store.data_dir, tmp_path / "not-published", key, "synthetic-ledger", confirmed_quiescent=True)
    assert not (tmp_path / "not-published").exists()


def test_restore_disk_failure_cleans_only_its_private_staging(tmp_path, state):
    _, key, _ = state
    archive, _ = make_backup(tmp_path, state)
    neighbor = tmp_path / "keep-neighbor"
    neighbor.mkdir();(neighbor / "keep").write_text("preserved")
    with patch("qa_agent.recovery.os.fsync", side_effect=OSError("synthetic full volume")):
        with pytest.raises(OSError): restore(archive, tmp_path / "not-published", key, "synthetic-ledger")
    assert not (tmp_path / "not-published").exists()
    assert (neighbor / "keep").read_text() == "preserved"
    assert not list(tmp_path.glob(".qa-recovery-*"))
