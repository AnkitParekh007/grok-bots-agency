"""License collection is factual review evidence, never an automatic terms decision."""
from email.message import Message
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import license_inventory as licenses

pytestmark = [pytest.mark.security, pytest.mark.audit]


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "installed"
    root.mkdir()
    values = Message()
    values["Name"], values["Version"] = "sample-package", "1.2.3"
    values["License-Expression"] = "MIT"
    values["License-File"] = "mit.LICENSE"
    values["License-File"] = "README.rst"
    names = ["sample_package-1.2.3.dist-info/mit.LICENSE", "sample_package-1.2.3.dist-info/README.rst",
             "sample_package/native/ThirdPartyNotices.txt"]
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Synthetic license evidence for collector tests.\n")
    dist = SimpleNamespace(version="1.2.3", metadata=values, files=names, locate_file=lambda rel: root / rel)
    lock = tmp_path / "requirements.lock"
    lock.write_text('sample-package==1.2.3\n  --hash=sha256:fixture\n', encoding="utf-8")
    return lock, root, dist


def collect(fixture):
    lock, _, dist = fixture
    return licenses.collect(lock, distribution=lambda name: dist)


def test_declared_nonstandard_and_observed_native_notices_are_retained(fixture):
    report, copies = collect(fixture)
    assert report["package_count"] == 1 and report["evidence_file_count"] == 3
    assert report["declared_files_complete"] is True
    assert report["owner_review"] == "NOT_REVIEWED" and report["release_eligible"] is False
    row = report["packages"][0]
    assert row["declared_license_expression"] == "MIT" and row["redistribution_approved"] is False
    assert not row["unresolved_license_files"]
    for item in row["evidence"]:
        assert hashlib.sha256(copies[item["output_path"]]).hexdigest() == item["sha256"]


def test_absent_declarations_and_notice_files_remain_explicit_unknowns(fixture):
    _, _, dist = fixture
    del dist.metadata["License-Expression"]
    dist.files = []
    report, copies = collect(fixture)
    assert report["unknown_declarations"] == report["missing_packaged_notices"] == ["sample-package"]
    assert report["declared_files_complete"] is False and report["release_eligible"] is False
    assert not copies


@pytest.mark.parametrize("mutation", ["version", "name", "traversal", "missing", "empty", "aliased", "oversized", "combined"])
def test_invalid_evidence_is_refused(fixture, mutation, monkeypatch):
    _, root, dist = fixture
    if mutation == "version": dist.version = "1.2.4"
    elif mutation == "name": dist.metadata.replace_header("Name", "other-package")
    elif mutation == "traversal": dist.metadata.replace_header("License-File", "../LICENSE")
    elif mutation == "missing": (root / dist.files[0]).unlink()
    elif mutation == "empty": (root / dist.files[0]).write_bytes(b"")
    elif mutation == "aliased": dist.files.append(dist.files[0].upper())
    elif mutation == "oversized": monkeypatch.setattr(licenses, "MAX_FILE", 2)
    elif mutation == "combined": monkeypatch.setattr(licenses, "MAX_TOTAL", 2)
    with pytest.raises(ValueError): collect(fixture)


@pytest.mark.parametrize("path", ["../LICENSE", "/LICENSE", "C:/LICENSE", "folder\\LICENSE", "CON.txt", "license/../data", "NUL", "LICENSE.", "LICENSE*", ".", "LICENSE\x00", None])
def test_portable_license_paths_cannot_escape_or_alias_devices(path):
    with pytest.raises(ValueError): licenses.safe_relative(path)


def test_unknown_legacy_license_is_not_converted_to_a_license_identity(fixture):
    _, _, dist = fixture
    del dist.metadata["License-Expression"]
    dist.metadata["License"] = "UNKNOWN"
    report, _ = collect(fixture)
    assert report["unknown_declarations"] == ["sample-package"]
    assert report["packages"][0]["declaration_present"] is True


def test_output_is_new_and_never_overwrites_an_existing_review(fixture, tmp_path, monkeypatch):
    report, copies = collect(fixture)
    monkeypatch.setattr(licenses, "collect", lambda lock: (report, copies))
    out = tmp_path / "report"
    licenses.write_report(fixture[0], out)
    original = (out / "inventory.json").read_bytes()
    with pytest.raises(FileExistsError): licenses.write_report(fixture[0], out)
    assert (out / "inventory.json").read_bytes() == original
