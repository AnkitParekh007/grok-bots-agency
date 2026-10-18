"""Dependency/interpreter binding and security controls found by static analysis."""
import subprocess
import sys
import json
import shutil
from unittest.mock import patch

import pytest

from qa_agent import dependencies
from qa_agent.runtime import QAAgent
from qa_agent.store import Store
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.security, pytest.mark.approval]


def test_installed_dependency_drift_changes_receipt_and_blocks_shared_startup(tmp_path, monkeypatch):
    before = dependencies.fingerprint()
    version = dependencies.importlib.metadata.version
    monkeypatch.setattr(dependencies.importlib.metadata, "version", lambda name: "0.0.0" if name == "fastapi" else version(name))
    after = dependencies.fingerprint()
    assert before != after and not after["matches_lock"]
    with pytest.raises(ValueError, match="locked dependency"):
        QAAgent(tmp_path, agent_cfg=agent_cfg("replay", deployment={"profile": "shared"}))


def test_store_rejects_sql_identifiers_outside_fixed_run_columns(tmp_path):
    store = Store(tmp_path)
    try:
        store.create_run("safe", "preserve", "synthetic-local", "replay", "REPLAYED")
        with pytest.raises(ValueError, match="immutable"):
            store.update_run("safe", **{"status='completed' WHERE 1=1 --": "untrusted"})
        with pytest.raises(ValueError, match="immutable"):
            store.update_run("safe", id="renamed")
        assert store.get_run("safe")["request"] == "preserve" and store.get_run("renamed") is None
    finally:
        store.db.close()


def test_timezone_grant_guard_remains_active_under_python_optimization(tmp_path):
    script = """
import sys
from pathlib import Path
from qa_agent.auth import Principal
from qa_agent.runtime import QAAgent
from qa_agent.eval_authorization import create_grant, AuthorizationError
from tests.b1_helpers import agent_cfg
a = QAAgent(Path(sys.argv[1]), agent_cfg=agent_cfg('replay'))
try:
    try:
        create_grant(a.store, a.gateway, Principal('synthetic-reviewer', 'approver', 'local'), {'expires_at':'2060-01-01T00:00:00'})
    except AuthorizationError as e:
        if 'timezone' not in str(e): raise
    else:
        raise RuntimeError('naive timestamp was accepted')
finally:
    a.store.db.close()
"""
    completed = subprocess.run([sys.executable, "-O", "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr


def test_lock_receipt_rejects_input_changes_without_regeneration(tmp_path):
    from scripts.verify_dependency_locks import ROOT, verify
    (tmp_path / "config").mkdir()
    shutil.copyfile(ROOT / "config/dependency-locks.json", tmp_path / "config/dependency-locks.json")
    receipt = json.loads((tmp_path / "config/dependency-locks.json").read_text())
    for name in receipt["files"]:
        shutil.copyfile(ROOT / name, tmp_path / name)
    verify(tmp_path)
    with (tmp_path / "requirements.txt").open("a") as output:
        output.write("\nchanged-package==0.0.0\n")
    with pytest.raises(ValueError, match="changed"):
        verify(tmp_path)


def test_secret_exception_policy_cannot_hide_raw_text_in_detector_metadata(tmp_path, monkeypatch):
    from scripts import scan_secrets
    (tmp_path / "config").mkdir()
    (tmp_path / "source.py").write_text("# synthetic policy contract\n")
    policy = {"format": "qa-secret-exceptions/1", "allowlist": [{"file": "source.py", "type": "unreviewed-arbitrary-text",
              "hashed_secret": "a" * 40, "category": "synthetic_fixture"}]}
    (tmp_path / scan_secrets.POLICY).write_text(json.dumps(policy))
    monkeypatch.setattr(scan_secrets, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="raw secrets"):
        scan_secrets.policy_entries()


@pytest.mark.parametrize("mutation", ["missing", "version", "duplicate", "vulnerability"])
def test_sbom_gate_rejects_incomplete_or_unsafe_inventory(tmp_path, mutation):
    from scripts.verify_sbom import verify
    lock, sbom = tmp_path / "lock.txt", tmp_path / "sbom.json"
    lock.write_text("pip==26.2.1\npackaging==26.3\n")
    obj = {"bomFormat": "CycloneDX", "components": [{"name": "pip", "version": "26.2.1"}, {"name": "packaging", "version": "26.3"}]}
    sbom.write_text(json.dumps(obj))
    assert verify(lock, sbom) == 2
    if mutation == "missing": obj["components"].pop()
    elif mutation == "version": obj["components"][0]["version"] = "26.1.2"
    elif mutation == "duplicate": obj["components"].append(obj["components"][0])
    else: obj["vulnerabilities"] = [{"id": "synthetic-advisory"}]
    sbom.write_text(json.dumps(obj))
    with pytest.raises(ValueError): verify(lock, sbom)
