"""SQLite task history + filesystem artifact store (runs/<run_id>/)."""
import json, sqlite3, threading
from pathlib import Path
from .interfaces import ArtifactStore, ApprovalProvider
from .util import now, sha256

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created TEXT, updated TEXT, status TEXT, request TEXT,
  story_key TEXT, environment TEXT, model_provider TEXT, model_label TEXT, story_json TEXT, ac_json TEXT,
  plan_json TEXT, plan_hash TEXT, summary_json TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, step TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS results(run_id TEXT, test_id TEXT, status TEXT, json TEXT, PRIMARY KEY(run_id, test_id));
CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, plan_hash TEXT,
  approver TEXT, decision TEXT, comment TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_id TEXT, tool TEXT,
  args_hash TEXT, args_summary TEXT, decision TEXT, reason TEXT, outcome TEXT, duration_ms INTEGER);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_update BEFORE UPDATE ON approvals BEGIN SELECT RAISE(ABORT,'approvals are append-only'); END;
"""


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.data_dir / "qa_agent.db", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Anything mid-flight when the process died is marked interrupted (no silent resume in Milestone A).
        self.db.execute("UPDATE runs SET status='interrupted', updated=? WHERE status IN ('planning','executing')", (now(),))

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)

    # runs
    def create_run(self, run_id, request, environment, provider, label):
        t = now()
        self.x("INSERT INTO runs(id,created,updated,status,request,environment,model_provider,model_label) VALUES(?,?,?,?,?,?,?,?)",
               (run_id, t, t, "planning", request, environment, provider, label))

    def update_run(self, run_id, **kw):
        kw["updated"] = now()
        for k, v in list(kw.items()):
            if isinstance(v, (dict, list)):
                kw[k] = json.dumps(v)
        cols = ",".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE runs SET {cols} WHERE id=?", (*kw.values(), run_id))

    def get_run(self, run_id):
        rows = self.q("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            return None
        r = rows[0]
        for k in ("story_json", "ac_json", "plan_json", "summary_json"):
            r[k] = json.loads(r[k]) if r[k] else None
        return r

    def list_runs(self):
        return self.q("SELECT id,created,status,story_key,environment,model_label,request FROM runs ORDER BY created DESC")

    def event(self, run_id, step, message):
        self.x("INSERT INTO events(run_id,ts,step,message) VALUES(?,?,?,?)", (run_id, now(), step, message))

    def events(self, run_id):
        return self.q("SELECT ts,step,message FROM events WHERE run_id=? ORDER BY id", (run_id,))

    def save_result(self, run_id, res):
        self.x("INSERT OR REPLACE INTO results VALUES(?,?,?,?)", (run_id, res["id"], res["status"], json.dumps(res)))

    def results(self, run_id):
        return [json.loads(r["json"]) for r in self.q("SELECT json FROM results WHERE run_id=? ORDER BY test_id", (run_id,))]

    def audit_rows(self, run_id):
        return self.q("SELECT * FROM audit WHERE run_id=? ORDER BY id", (run_id,))


class FileArtifactStore(ArtifactStore):
    def __init__(self, runs_dir: Path):
        self.root = Path(runs_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, run_id, rel=""):
        p = (self.root / run_id / rel).resolve()
        if not str(p).startswith(str(self.root / run_id)):
            raise ValueError("path escapes run directory")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_bytes(self, run_id, rel, data):
        p = self.path_for(run_id, rel)
        p.write_bytes(data)
        return str(p)

    def manifest(self, run_id):
        base = self.root / run_id
        out = []
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.name != "manifest.json":
                out.append({"path": str(p.relative_to(base)), "bytes": p.stat().st_size, "sha256": sha256(p.read_bytes())})
        return out


class SqliteApprovalProvider(ApprovalProvider):
    def __init__(self, store: Store):
        self.store = store

    def record(self, run_id, plan_hash, approver, decision, comment=""):
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved|rejected")
        if not approver:
            raise ValueError("approver required")
        self.store.x("INSERT INTO approvals(run_id,ts,plan_hash,approver,decision,comment) VALUES(?,?,?,?,?,?)",
                     (run_id, now(), plan_hash, approver, decision, comment))
        return {"run_id": run_id, "plan_hash": plan_hash, "approver": approver, "decision": decision}

    def latest(self, run_id):
        r = self.store.q("SELECT * FROM approvals WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,))
        return r[0] if r else None

    def is_approved(self, run_id, plan_hash):
        a = self.latest(run_id)  # fail closed: no record, rejected, or hash mismatch => False
        return bool(a and a["decision"] == "approved" and plan_hash and a["plan_hash"] == plan_hash)
