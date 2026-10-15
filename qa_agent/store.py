"""SQLite task history + filesystem artifact store (runs/<run_id>/)."""
import json, sqlite3, threading
from pathlib import Path
from .interfaces import ArtifactStore, ApprovalProvider
from datetime import datetime, timezone
from .util import now, sha256, canon, redact

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
CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, day TEXT, provider TEXT,
  model TEXT, task TEXT, attempt INTEGER, prompt_tokens INTEGER, cached_tokens INTEGER, completion_tokens INTEGER,
  reasoning_tokens INTEGER, total_tokens INTEGER, cost_usd REAL, priced INTEGER, outcome TEXT);
CREATE TABLE IF NOT EXISTS eval_grants(grant_id TEXT PRIMARY KEY, created_by TEXT, created_at TEXT, stories TEXT, environment TEXT,
  skills TEXT, actions TEXT, model_classes TEXT, expires_at TEXT, max_uses INTEGER, uses INTEGER DEFAULT 0,
  require_first_manual_approval INTEGER, purpose TEXT, grant_hash TEXT, revoked_at TEXT, revoked_by TEXT);
CREATE TABLE IF NOT EXISTS eval_grant_uses(id INTEGER PRIMARY KEY AUTOINCREMENT, grant_id TEXT, run_id TEXT UNIQUE, ts TEXT);
CREATE TABLE IF NOT EXISTS telemetry(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, kind TEXT, provider TEXT, detail TEXT);
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
        self._migrate()
        # Anything mid-flight when the process died is marked interrupted (no silent resume in Milestone A).
        stale = [r[0] for r in self.db.execute("SELECT id FROM runs WHERE status IN ('planning','executing')")]
        for rid in stale:
            self.db.execute("UPDATE runs SET status='interrupted', updated=?, error=? WHERE id=?",
                            (now(), "process stopped while run was in progress; partial results and evidence preserved; re-run required", rid))
            self.db.execute("INSERT INTO events(run_id,ts,step,message) VALUES(?,?,?,?)",
                            (rid, now(), "interrupted", "Agent restarted while this run was in progress. Marked INTERRUPTED (not resumed, not passed)."))

    def _migrate(self):
        def cols(t):
            return {r[1] for r in self.db.execute(f"PRAGMA table_info({t})")}
        add = {"runs": [("planning_json", "TEXT"), ("requested_by", "TEXT"), ("cancel_requested", "INTEGER DEFAULT 0"),
                        ("catalog_version", "TEXT"), ("plan_created_at", "TEXT"),
                        ("project", "TEXT"), ("repository", "TEXT"), ("repo_context_json", "TEXT")],
               "approvals": [("identity", "TEXT"), ("auth_mode", "TEXT"), ("approver_label", "TEXT")],
               "audit": [("prev_hash", "TEXT"), ("entry_hash", "TEXT"),
                          ("seq", "INTEGER"), ("payload_hash", "TEXT")],
               "usage": [("http_attempt", "INTEGER"), ("est_usd", "REAL"), ("budget_scope", "TEXT")]}
        for t, cs in add.items():
            have = cols(t)
            for c, typ in cs:
                if c not in have:
                    self.db.execute(f"ALTER TABLE {t} ADD COLUMN {c} {typ}")

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
        if kw.get("error"):
            kw["error"] = redact(kw["error"])
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
        for k in ("story_json", "ac_json", "plan_json", "summary_json", "planning_json", "repo_context_json"):
            r[k] = json.loads(r[k]) if r[k] else None
        return r

    def list_runs(self):
        return self.q("SELECT id,created,status,story_key,environment,model_label,request,requested_by FROM runs ORDER BY created DESC")

    def event(self, run_id, step, message):
        self.x("INSERT INTO events(run_id,ts,step,message) VALUES(?,?,?,?)", (run_id, now(), step, redact(message)))

    # ---- audit (append-only, hash-chained) ----
    AUDIT_FIELDS = ("ts", "run_id", "tool", "args_hash", "args_summary", "decision", "reason", "outcome", "duration_ms")

    @staticmethod
    def entry_hash(prev_hash, rec):
        return sha256((prev_hash or "GENESIS") + "|" + canon({k: rec.get(k) for k in Store.AUDIT_FIELDS}))

    @staticmethod
    def payload_hash(rec):
        """Canonical payload hash: sorted-key compact JSON of the audited fields."""
        return sha256(canon({k: rec.get(k) for k in Store.AUDIT_FIELDS}))

    @staticmethod
    def entry_hash_v2(seq, prev_hash, payload_hash):
        return sha256(f"audit-v2|{seq}|{prev_hash}|{payload_hash}")

    def append_audit(self, rec):
        """Caller must hold the gateway lock. v2 chain: monotonically increasing seq + canonical payload hash."""
        with self.lock:
            last = self.db.execute("SELECT entry_hash, seq FROM audit WHERE entry_hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
            last_seq = self.db.execute("SELECT MAX(seq) FROM audit").fetchone()[0] or 0
            rec["prev_hash"] = last[0] if last else "GENESIS"
            rec["seq"] = last_seq + 1
            rec["payload_hash"] = self.payload_hash(rec)
            rec["entry_hash"] = self.entry_hash_v2(rec["seq"], rec["prev_hash"], rec["payload_hash"])
            self.db.execute("INSERT INTO audit(ts,run_id,tool,args_hash,args_summary,decision,reason,outcome,duration_ms,prev_hash,entry_hash,seq,payload_hash)"
                            " VALUES(:ts,:run_id,:tool,:args_hash,:args_summary,:decision,:reason,:outcome,:duration_ms,:prev_hash,:entry_hash,:seq,:payload_hash)", rec)
        return rec

    def export_audit(self, path):
        return export_audit(self.q("SELECT * FROM audit ORDER BY id"), path)

    def verify_audit_chain(self):
        rows = self.q("SELECT * FROM audit ORDER BY id")
        return verify_chain(rows)

    # ---- usage / telemetry ----
    def add_usage(self, run_id, provider, model, task, attempt, u, cost, priced, outcome):
        """u=None means the provider reported no usage: token columns are stored NULL (never invented)."""
        if u is None:
            self.x("INSERT INTO usage(run_id,ts,day,provider,model,task,attempt,cost_usd,priced,outcome) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (run_id, now(), datetime.now(timezone.utc).date().isoformat(), provider, model, task, attempt, cost, 0, outcome))
            return
        self.x("INSERT INTO usage(run_id,ts,day,provider,model,task,attempt,prompt_tokens,cached_tokens,completion_tokens,"
               "reasoning_tokens,total_tokens,cost_usd,priced,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (run_id, now(), datetime.now(timezone.utc).date().isoformat(), provider, model, task, attempt,
                u.get("prompt_tokens", 0), u.get("cached_tokens", 0), u.get("completion_tokens", 0), u.get("reasoning_tokens", 0),
                u.get("total_tokens", 0), cost, 1 if priced else 0, outcome))

    def reserve_spend(self, run_id, provider, model, task, attempt, http_attempt, est, per_run_cap, daily_cap,
                      total_cap=None, scope=None):
        """Atomically check caps and insert a 'reserved' row charged at the worst-case estimate.
        BEGIN IMMEDIATE takes SQLite's write lock, so concurrent reservations (threads or processes sharing this DB)
        are serialized: two runs cannot both pass the cap check against the same remaining budget."""
        day = datetime.now(timezone.utc).date().isoformat()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                run_spent = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE run_id=?", (run_id,)).fetchone()[0]
                day_spent = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE day=?", (day,)).fetchone()[0]
                reason = None
                if run_spent + est > per_run_cap:
                    reason = f"per-run cap ${per_run_cap} would be exceeded (spent ${run_spent:.4f} + est ${est:.4f})"
                elif day_spent + est > daily_cap:
                    reason = f"daily cap ${daily_cap} would be exceeded (spent ${day_spent:.4f} + est ${est:.4f})"
                elif total_cap is not None:
                    total = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE budget_scope=?", (scope,)).fetchone()[0]
                    if total + est > total_cap:
                        reason = f"total cap ${total_cap} for scope {scope!r} would be exceeded (spent ${total:.4f} + est ${est:.4f})"
                if reason:
                    self.db.execute("ROLLBACK")
                    return None, reason
                cur = self.db.execute(
                    "INSERT INTO usage(run_id,ts,day,provider,model,task,attempt,http_attempt,cost_usd,est_usd,priced,outcome,budget_scope)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,0,'reserved',?)",
                    (run_id, now(), day, provider, model, task, attempt, http_attempt, est, est, scope))
                self.db.execute("COMMIT")
                return cur.lastrowid, None
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def settle_spend(self, uid, u, cost, priced, outcome):
        """Replace a reservation with the actual charge. u=None: usage unknown -> token columns stay NULL and the
        caller passes the conservative reserved estimate (or 0.0 when the request provably never left the process)."""
        if u is None:
            self.x("UPDATE usage SET cost_usd=?, priced=0, outcome=? WHERE id=?", (cost, outcome, uid))
        else:
            self.x("UPDATE usage SET prompt_tokens=?, cached_tokens=?, completion_tokens=?, reasoning_tokens=?, total_tokens=?,"
                   " cost_usd=?, priced=?, outcome=? WHERE id=?",
                   (u.get("prompt_tokens"), u.get("cached_tokens"), u.get("completion_tokens"), u.get("reasoning_tokens"),
                    u.get("total_tokens"), cost, 1 if priced else 0, outcome, uid))

    def run_usage(self, run_id):
        rows = self.q("SELECT * FROM usage WHERE run_id=? ORDER BY id", (run_id,))
        tot = {k: sum((r[k] or 0) for r in rows) for k in ("prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens", "total_tokens")}
        tot["cost_usd"] = round(sum(r["cost_usd"] or 0 for r in rows), 6)
        tot["calls"] = len(rows)
        return {"totals": tot, "calls": rows}

    GRANT_LISTS = ("stories", "skills", "actions", "model_classes")

    def insert_grant(self, g):
        row = {**g, **{k: json.dumps(g[k]) for k in self.GRANT_LISTS}, "require_first_manual_approval": 1 if g["require_first_manual_approval"] else 0}
        self.x("INSERT INTO eval_grants(grant_id,created_by,created_at,stories,environment,skills,actions,model_classes,expires_at,"
               "max_uses,uses,require_first_manual_approval,purpose,grant_hash) VALUES(:grant_id,:created_by,:created_at,:stories,"
               ":environment,:skills,:actions,:model_classes,:expires_at,:max_uses,0,:require_first_manual_approval,:purpose,:grant_hash)", row)

    def get_grant(self, grant_id):
        r = self.q("SELECT * FROM eval_grants WHERE grant_id=?", (grant_id,))
        if not r:
            return None
        g = dict(r[0])
        for k in self.GRANT_LISTS:
            g[k] = json.loads(g[k])
        g["require_first_manual_approval"] = bool(g["require_first_manual_approval"])
        return g

    def revoke_grant(self, grant_id, by):
        with self.lock:
            cur = self.db.execute("UPDATE eval_grants SET revoked_at=?, revoked_by=? WHERE grant_id=? AND revoked_at IS NULL", (now(), by, grant_id))
            return cur.rowcount == 1

    def claim_grant_use(self, grant_id, run_id):
        """Atomic: increments uses only if not revoked and under the limit (BEGIN IMMEDIATE). Returns (ok, uses_before)."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                before = self.db.execute("SELECT uses FROM eval_grants WHERE grant_id=?", (grant_id,)).fetchone()
                cur = self.db.execute("UPDATE eval_grants SET uses=uses+1 WHERE grant_id=? AND revoked_at IS NULL AND uses < max_uses", (grant_id,))
                if cur.rowcount != 1:
                    self.db.execute("ROLLBACK")
                    return False, before[0] if before else None
                self.db.execute("INSERT INTO eval_grant_uses(grant_id,run_id,ts) VALUES(?,?,?)", (grant_id, run_id, now()))
                self.db.execute("COMMIT")
                return True, before[0]
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def has_manual_authenticated_approval(self, model_class_prefix):
        rows = self.q("SELECT r.model_label FROM approvals a JOIN runs r ON r.id=a.run_id WHERE a.decision='approved' "
                      "AND a.auth_mode='local' AND a.identity IS NOT NULL")
        return any((x["model_label"] or "").split(" (")[0].strip() == model_class_prefix for x in rows)

    def scope_spend(self, scope):
        return self.q("SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE budget_scope=?", (scope,))[0]["s"]

    def run_spend(self, run_id):
        return self.q("SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE run_id=?", (run_id,))[0]["s"]

    def day_spend(self, day=None):
        day = day or datetime.now(timezone.utc).date().isoformat()
        return self.q("SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE day=?", (day,))[0]["s"]

    def telemetry(self, run_id, kind, provider, detail):
        self.x("INSERT INTO telemetry(run_id,ts,kind,provider,detail) VALUES(?,?,?,?,?)", (run_id, now(), kind, provider, redact(detail)[:500]))

    def telemetry_summary(self):
        return self.q("SELECT kind, provider, COUNT(*) n FROM telemetry GROUP BY kind, provider ORDER BY kind")

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

    def record(self, run_id, plan_hash, approver, decision, comment="", identity=None, auth_mode="demo", approver_label=None):
        """approver = name shown on reports. identity = authenticated principal (None in demo mode = unauthenticated label)."""
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved|rejected")
        if not approver:
            raise ValueError("approver required")
        self.store.x("INSERT INTO approvals(run_id,ts,plan_hash,approver,decision,comment,identity,auth_mode,approver_label) VALUES(?,?,?,?,?,?,?,?,?)",
                     (run_id, now(), plan_hash, approver, decision, comment, identity, auth_mode, approver_label))
        return {"run_id": run_id, "plan_hash": plan_hash, "approver": approver, "decision": decision, "identity": identity}

    def latest(self, run_id):
        r = self.store.q("SELECT * FROM approvals WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,))
        return r[0] if r else None

    def is_approved(self, run_id, plan_hash):
        a = self.latest(run_id)  # fail closed: no record, rejected, or hash mismatch => False
        return bool(a and a["decision"] == "approved" and plan_hash and a["plan_hash"] == plan_hash)


def verify_chain(rows):
    """Recompute the audit hash chain.
    legacy rows (Milestone A, no entry_hash) are counted, not verified; v1 rows (B1, no seq) use the B1 formula;
    v2 rows require seq == previous v2 seq + 1, a matching canonical payload hash and entry hash."""
    prev, legacy, checked, last_seq = None, 0, 0, None
    for r in rows:
        rid = r.get("id")
        if not r.get("entry_hash"):
            if checked:
                return {"ok": False, "broken_at_id": rid, "reason": "unchained row after chain start", "checked": checked, "legacy": legacy}
            legacy += 1
            continue
        if r.get("prev_hash") != (prev or "GENESIS"):
            return {"ok": False, "broken_at_id": rid, "reason": "prev_hash mismatch (row removed/reordered)", "checked": checked, "legacy": legacy}
        if r.get("seq") is None:
            if last_seq is not None:
                return {"ok": False, "broken_at_id": rid, "reason": "unsequenced row after sequenced rows", "checked": checked, "legacy": legacy}
            if Store.entry_hash(r["prev_hash"], r) != r["entry_hash"]:
                return {"ok": False, "broken_at_id": rid, "reason": "entry_hash mismatch (row modified)", "checked": checked, "legacy": legacy}
        else:
            expect = (last_seq or 0) + 1
            if r["seq"] != expect:
                return {"ok": False, "broken_at_id": rid, "reason": f"incorrect sequence: got {r['seq']}, expected {expect}", "checked": checked, "legacy": legacy}
            ph = Store.payload_hash(r)
            if ph != r.get("payload_hash") or Store.entry_hash_v2(r["seq"], r["prev_hash"], ph) != r["entry_hash"]:
                return {"ok": False, "broken_at_id": rid, "reason": "entry_hash mismatch (row modified)", "checked": checked, "legacy": legacy}
            last_seq = r["seq"]
        prev = r["entry_hash"]
        checked += 1
    return {"ok": True, "checked": checked, "legacy": legacy, "head": prev, "last_seq": last_seq}


EXPORT_FIELDS = ("id", *Store.AUDIT_FIELDS, "prev_hash", "entry_hash", "seq", "payload_hash")


def export_audit(rows, path):
    """Write a self-verifying JSONL export: one canonical line per row + a final manifest line."""
    lines = [canon({k: r.get(k) for k in EXPORT_FIELDS}) for r in rows]
    body = "".join(l + "\n" for l in lines)
    v = verify_chain(rows)
    manifest = {"_manifest": {"format": "qa-audit-export/1", "count": len(rows), "body_sha256": sha256(body),
                              "head": v.get("head"), "last_seq": v.get("last_seq"), "chain_ok_at_export": v["ok"],
                              "exported_at": now()}}
    Path(path).write_text(body + canon(manifest) + "\n")
    return manifest["_manifest"]


def verify_export(path):
    text = Path(path).read_text()
    lines = text.splitlines()
    if not lines:
        return {"ok": False, "reason": "empty export"}
    try:
        man = json.loads(lines[-1])["_manifest"]
    except (ValueError, KeyError):
        return {"ok": False, "reason": "missing manifest"}
    body = "".join(l + "\n" for l in lines[:-1])
    if sha256(body) != man["body_sha256"]:
        return {"ok": False, "reason": "body hash mismatch (export modified)"}
    rows = [json.loads(l) for l in lines[:-1]]
    if len(rows) != man["count"]:
        return {"ok": False, "reason": "row count mismatch"}
    v = verify_chain(rows)
    if v["ok"] and v.get("head") != man["head"]:
        return {"ok": False, "reason": "head mismatch"}
    return {**v, "manifest": man}
