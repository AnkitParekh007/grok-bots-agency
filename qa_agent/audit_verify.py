"""Verify the audit hash chain in SQLite and in the global JSONL.  python -m qa_agent.audit_verify [--data-dir data]"""
import argparse, json, sqlite3, sys
from pathlib import Path
from .store import verify_chain


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    r = verify_data_dir(Path(ap.parse_args().data_dir))
    print(json.dumps(r, indent=2))
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
