"""Offline detect-secrets gate with exact reviewed exceptions, never network verification."""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
POLICY = "config/secret-scan-allowlist.json"
CATEGORIES = {"public_digest", "environment_reference", "synthetic_fixture"}
TYPES = {"Hex High Entropy String", "Base64 High Entropy String", "Secret Keyword"}


def policy_entries():
    obj = json.loads((ROOT / POLICY).read_text(encoding="utf-8"))
    if set(obj) != {"format", "allowlist"} or obj["format"] != "qa-secret-exceptions/1" or not isinstance(obj["allowlist"], list):
        raise ValueError("invalid exact secret exception policy")
    allowed = set()
    for row in obj["allowlist"]:
        if set(row) != {"file", "type", "hashed_secret", "category"} or row["category"] not in CATEGORIES or row["type"] not in TYPES or not re.fullmatch("[0-9a-f]{40}", row["hashed_secret"]):
            raise ValueError("invalid exception fields; raw secrets are prohibited")
        path = PurePosixPath(row["file"])
        if path.is_absolute() or ".." in path.parts or str(path) != row["file"] or not (ROOT / row["file"]).is_file():
            raise ValueError("exception refers to an invalid repository file")
        allowed.add((row["file"], row["type"], row["hashed_secret"]))
    return allowed


def scan(paths):
    result = []
    # Batches also fit the Windows command-line bound. All arguments are filenames;
    # '--' prevents a repository filename from becoming an option. No shell is used.
    for start in range(0, len(paths), 40):
        call = subprocess.run([sys.executable, "-m", "detect_secrets", "scan", "--no-verify", "--all-files", "--", *paths[start:start + 40]],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
        if call.returncode:
            raise RuntimeError("offline secret detector failed; no pass receipt generated")
        baseline = json.loads(call.stdout)
        for filename, rows in baseline["results"].items():
            for row in rows:
                result.append({"file": filename.replace("\\", "/"), **row})
    return result


def self_test():
    # Deliberately invalid, generated fixture value; never sent to any provider.
    token = "AK" + "IA" + secrets.token_hex(8).upper()
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=ROOT, prefix=".secret-scan-selftest-", suffix=".txt", delete=False) as output:
        path = Path(output.name)
        output.write("aws_access_key_id = " + repr(token) + "\n")
    try:
        hits = scan([path.relative_to(ROOT).as_posix()])
        if not hits:
            raise RuntimeError("secret canary was not detected; guard is not qualified")
        print("Offline secret guard rejected the synthetic canary.")
    finally:
        if path.resolve().parent != ROOT.resolve() or not path.name.startswith(".secret-scan-selftest-"):
            raise RuntimeError("refusing cleanup outside generated canary file")
        path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/security/secret-scan.json")
    args = parser.parse_args()
    if args.self_test:
        self_test();return
    names = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=ROOT, text=True, encoding="utf-8", timeout=30).split("\0")
    paths, total = [], 0
    for name in filter(None, names):
        path = ROOT / name
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT) or not path.is_file():
            raise ValueError("secret scan requires isolated regular tracked files")
        total += path.stat().st_size
        if path.stat().st_size > 20_000_000 or total > 500_000_000 or len(paths) >= 10000:
            raise ValueError("secret scan exceeds repository file/byte budget")
        if name != POLICY:  # Policy schema allows only fixed categories and one-way digests.
            paths.append(name)
    allowed = policy_entries()
    rows = scan(paths)
    observed = {(row["file"], row["type"], row["hashed_secret"]) for row in rows}
    stale = allowed - observed
    unexpected = [{"file": row["file"], "type": row["type"], "line": row["line_number"]} for row in rows
                  if (row["file"], row["type"], row["hashed_secret"]) not in allowed]
    report = {"format": "qa-offline-secret-scan/1", "network_verification": False, "scanned_files": len(paths),
              "accepted_known_findings": len(rows) - len(unexpected), "stale_exceptions": len(stale), "unexpected": unexpected}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    if unexpected or stale:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
