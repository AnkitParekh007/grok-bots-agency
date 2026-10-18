"""Require every applicable exact locked package/version in the scanner's SBOM."""
import argparse
import json
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def verify(lock, sbom):
    expected = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        req = Requirement(line.rstrip().removesuffix("\\").strip())
        if req.url or len(req.specifier) != 1 or next(iter(req.specifier)).operator != "==" or "*" in next(iter(req.specifier)).version:
            raise ValueError("SBOM gate requires exact named locked versions")
        if not req.marker or req.marker.evaluate():
            name = canonicalize_name(req.name)
            if name in expected:
                raise ValueError("ambiguous applicable dependency entries")
            expected[name] = str(Version(next(iter(req.specifier)).version))
    obj = json.loads(sbom.read_text(encoding="utf-8"))
    if obj.get("bomFormat") != "CycloneDX" or not isinstance(obj.get("components"), list):
        raise ValueError("missing CycloneDX component inventory")
    actual = {canonicalize_name(row["name"]): str(Version(row["version"])) for row in obj["components"]}
    if not expected or expected != actual or len(actual) != len(obj["components"]):
        raise ValueError("SBOM packages/versions differ from the complete applicable lock")
    if obj.get("vulnerabilities"):
        raise ValueError("SBOM contains known vulnerabilities; remediation required")
    return len(expected)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    parser.add_argument("sbom", type=Path)
    args = parser.parse_args()
    print(f"SBOM covers {verify(args.lock, args.sbom)} exact applicable locked packages; no reported vulnerabilities.")
