"""Fail CI if either reviewed dependency input or its generated lock changed alone."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(root=ROOT):
    manifest = json.loads((root / "config/dependency-locks.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "qa-dependency-locks/1" or set(manifest.get("files", {})) != {"requirements.txt", "requirements.lock", "requirements-security.in", "requirements-security.lock"}:
        raise ValueError("dependency lock receipt has invalid file scope")
    for name, expected in manifest["files"].items():
        path = root / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("dependency input/lock changed; regenerate and review the complete lock receipt")
    return manifest


if __name__ == "__main__":
    verified = verify()
    print("Dependency inputs and both hashed locks match their reviewed receipt.")
