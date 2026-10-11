"""Verify the audit hash chain in SQLite and in the global JSONL.  python -m qa_agent.audit_verify [--data-dir data]"""
import argparse, json, sqlite3, sys
from pathlib import Path
from .store import verify_chain, export_audit, verify_export


def verify_data_dir(data_dir: Path):
    db = sqlite3.connect(Path(data_dir) / "qa_agent.db")
    db.row_factory = sqlite3.Row
    sql = verify_chain([dict(r) for r in db.execute("SELECT * FROM audit ORDER BY id")])
    jl = Path(data_dir) / "audit.jsonl"
    lines = [json.loads(l) for l in jl.read_text().splitlines()] if jl.exists() else []
    for i, l in enumerate(lines):
        l.setdefault("id", i + 1)
    jsonl = verify_chain(lines)
    return {"sqlite": sql, "jsonl": jsonl, "ok": sql["ok"] and jsonl["ok"]}


def main():
    """python -m qa_agent.audit_verify [--data-dir data]                       verify DB + JSONL chains
       python -m qa_agent.audit_verify export --data-dir data --out FILE      write self-verifying export
       python -m qa_agent.audit_verify verify-export FILE                     verify an export"""
    argv = sys.argv[1:]
    cmd = argv.pop(0) if argv and argv[0] in ("export", "verify-export") else "verify"
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out")
    ap.add_argument("file", nargs="?")
    a = ap.parse_args(argv)
    if cmd == "export":
        db = sqlite3.connect(Path(a.data_dir) / "qa_agent.db"); db.row_factory = sqlite3.Row
        r = export_audit([dict(x) for x in db.execute("SELECT * FROM audit ORDER BY id")], a.out)
        r["ok"] = r["chain_ok_at_export"]
    elif cmd == "verify-export":
        r = verify_export(a.file)
    else:
        r = verify_data_dir(Path(a.data_dir))
    print(json.dumps(r, indent=2))
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
