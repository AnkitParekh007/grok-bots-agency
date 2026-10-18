"""Collect installed locked-package license evidence, without approving any terms."""
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path, PurePosixPath
import re
import sys

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

MAX_FILE = 1_000_000
MAX_TOTAL = 20_000_000


def bounded(path, limit):
    with path.open("rb") as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ValueError("license evidence exceeds its byte limit")
    return body


def safe_relative(value):
    if not isinstance(value, str):
        raise ValueError("unsafe license evidence path")
    path = PurePosixPath(value)
    if (not value or not path.parts or len(value) > 1024 or "\\" in value or ":" in value or path.is_absolute()
            or any(ord(char) < 32 for char in value)
            or any(part in (".", "..") or len(part) > 256 or part != part.strip() or part.endswith(".")
                   or any(char in part for char in '<>"|?*')
                   or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", part.split(".", 1)[0]) for part in path.parts)):
        raise ValueError("unsafe license evidence path")
    return path


def locked(lock):
    body = bounded(lock, 2_000_000)
    expected = {}
    for line in body.decode("utf-8").splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        req = Requirement(line.rstrip().removesuffix("\\").strip())
        pins = list(req.specifier)
        if req.url or len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
            raise ValueError("license evidence requires exact named locked packages")
        if req.marker and not req.marker.evaluate():
            continue
        name = canonicalize_name(req.name)
        if name in expected or len(expected) >= 256:
            raise ValueError("ambiguous or excessive locked package inventory")
        expected[name] = str(Version(pins[0].version))
    if not expected:
        raise ValueError("license evidence lock has no applicable packages")
    return expected, hashlib.sha256(body).hexdigest()


def collect(lock, *, distribution=metadata.distribution):
    expected, lock_hash = locked(Path(lock))
    rows, copies, total = [], {}, 0
    for name, version in sorted(expected.items()):
        dist = distribution(name)
        if str(Version(dist.version)) != version or canonicalize_name(dist.metadata["Name"]) != name:
            raise ValueError("installed license evidence differs from the applicable lock")
        values = dist.metadata
        expression, legacy = values.get("License-Expression"), values.get("License")
        classifiers = [v for v in values.get_all("Classifier", []) if v.startswith("License ::")]
        declared = values.get_all("License-File", [])
        if (any(v is not None and (not isinstance(v, str) or len(v) > 20000) for v in (expression, legacy))
                or len(classifiers) > 32 or any(len(v) > 1000 for v in classifiers) or len(declared) > 128):
            raise ValueError("license declaration metadata exceeds bounds")
        declared = [str(safe_relative(value)) for value in declared]
        record = list(dist.files or [])
        if len(record) > 50000:
            raise ValueError("installed package file record exceeds inventory bound")
        candidates = []
        for item in record:
            text = str(item).replace("\\", "/")
            folded = text.casefold()
            leaf = PurePosixPath(text).name.casefold()
            is_declared = any(folded.endswith((".dist-info/licenses/" + value).casefold()) or folded.endswith((".dist-info/" + value).casefold()) for value in declared)
            heuristic = (re.match(r"^(licen[cs]es?|copying|copyright|notices?|thirdpartynotices?|authors)([.\-_]|$)", leaf)
                         and PurePosixPath(leaf).suffix not in (".py", ".pyc", ".js", ".ts", ".so", ".pyd", ".dll", ".exe"))
            if ".dist-info/licenses/" in folded or is_declared or heuristic:
                candidates.append(str(safe_relative(text)))
        if len(candidates) > 128 or len({v.casefold() for v in candidates}) != len(candidates):
            raise ValueError("excessive or aliased license evidence paths")
        root = Path(dist.locate_file("")).resolve()
        evidence = []
        for relative in sorted(candidates):
            path = Path(dist.locate_file(relative))
            if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
                raise ValueError("license evidence is missing or outside the installed package root")
            body = bounded(path, MAX_FILE)
            if not body:
                raise ValueError("packaged license evidence is empty")
            total += len(body)
            if total > MAX_TOTAL:
                raise ValueError("combined license evidence exceeds its byte limit")
            output = str(PurePosixPath("notices", name + "-" + version, relative))
            copies[output] = body
            evidence.append({"source_relative_path": relative, "output_path": output,
                             "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        unresolved = [value for value in declared if not any(
            item["source_relative_path"].endswith(".dist-info/licenses/" + value)
            or (".dist-info/" in item["source_relative_path"] and item["source_relative_path"].split(".dist-info/", 1)[1] == value)
            for item in evidence)]
        identified = bool(classifiers) or any(v and v.strip().upper() not in ("UNKNOWN", "NOASSERTION", "NONE") for v in (expression, legacy))
        rows.append({"name": name, "version": version, "declared_license_expression": expression,
                     "declared_legacy_license": legacy, "license_classifiers": classifiers,
                     "declared_license_files": declared, "unresolved_license_files": unresolved,
                     "evidence": evidence, "declaration_present": bool(expression or legacy or classifiers),
                     "license_identity_status": "DECLARED_UNVERIFIED" if identified else "UNKNOWN",
                     "owner_review": "NOT_REVIEWED", "redistribution_approved": False})
    report = {"format": "qa-python-license-evidence/1", "lock_sha256": lock_hash,
              "platform": sys.platform, "python": sys.version.split()[0], "packages": rows,
              "package_count": len(rows), "evidence_file_count": len(copies), "evidence_bytes": total,
              "declared_files_complete": all(not row["unresolved_license_files"] for row in rows),
              "owner_review": "NOT_REVIEWED", "release_eligible": False,
              "scope": "Installed Python lock entries and observed packaged notices; native/OS/browser/model/service terms are separate",
              "unknown_declarations": [row["name"] for row in rows if row["license_identity_status"] == "UNKNOWN"],
              "missing_packaged_notices": [row["name"] for row in rows if not row["evidence"]]}
    return report, copies


def write_report(lock, out):
    report, copies = collect(lock)
    encoded = json.dumps(report, indent=2, ensure_ascii=True).encode() + b"\n"
    if len(encoded) > 2_000_000:
        raise ValueError("license evidence manifest exceeds its byte limit")
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    for relative, body in copies.items():
        path = out.joinpath(*safe_relative(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(body)
    with (out / "inventory.json").open("xb") as stream:
        stream.write(encoded)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    report = write_report(args.lock, args.out)
    print(json.dumps({k: report[k] for k in ("package_count", "evidence_file_count", "declared_files_complete", "unknown_declarations", "owner_review", "release_eligible")}))
    if not report["declared_files_complete"]:
        raise SystemExit("declared license files could not all be located; review package evidence")


if __name__ == "__main__":
    main()
