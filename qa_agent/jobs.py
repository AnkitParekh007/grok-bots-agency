"""SQLite durable queue, bounded admission, leases and fencing.

Expired running work is interrupted, never automatically replayed. A new request
and review are required for uncertain mutations. Queue payloads contain references,
identity/scope receipts and hashes, never arbitrary callables or secret values.
"""
import json
import time
import uuid
from contextlib import contextmanager

from .util import canon, sha256, now, redact


class JobConflict(ValueError):
    pass


class LeaseLost(JobConflict):
    pass


class JobCancelled(JobConflict):
    pass


class DurableJobs:
    def __init__(self, store, cfg=None, *, clock=time.time):
        self.store, self.cfg, self.clock = store, cfg or {}, clock
        self.lease_s = float(self.cfg.get("lease_s", 60))
        self.max_job_s = float(self.cfg.get("max_job_s", 1800))
        self.max_queued = int(self.cfg.get("max_queued", 100))
        self.max_per_tenant = int(self.cfg.get("max_per_tenant", 50))
        self.max_per_requester = int(self.cfg.get("max_per_requester", 20))
        self.running = {"planning": int(self.cfg.get("planning_workers", 2)), "execution": int(self.cfg.get("execution_workers", 1))}
        if not 10 <= self.lease_s <= 600 or not 60 <= self.max_job_s <= 86400 or any(not 1 <= v <= 10000 for v in (self.max_queued, self.max_per_tenant, self.max_per_requester)) or any(not 1 <= v <= 16 for v in self.running.values()):
            raise ValueError("invalid durable job admission/lease bounds")

    @contextmanager
    def transaction(self):
        with self.store.lock:
            outer = self.store.db.in_transaction
            self.store.db.execute("SAVEPOINT job_operation" if outer else "BEGIN IMMEDIATE")
            try:
                yield self.store.db
                self.store.db.execute("RELEASE job_operation" if outer else "COMMIT")
            except Exception:
                self.store.db.execute("ROLLBACK TO job_operation" if outer else "ROLLBACK")
                if outer:
                    self.store.db.execute("RELEASE job_operation")
                raise

    def _audit(self, job, decision, reason):
        args = {key: job.get(key) for key in ("id", "run_id", "kind", "fence", "state")}
        self.store.append_audit({"ts": now(), "run_id": job["run_id"], "tool": "job.transition", "args_hash": sha256(canon(args)),
                                 "args_summary": canon(args), "decision": decision, "reason": reason, "outcome": job["state"], "duration_ms": 0})

    def enqueue(self, run_id, kind, payload, config_hash, *, resources=(), expires_at=None):
        if kind not in self.running or not isinstance(payload, dict) or len(canon(payload).encode()) > 64000:
            raise JobConflict("invalid bounded job kind/payload")
        if not isinstance(config_hash, str) or len(config_hash) != 64 or any(c not in "0123456789abcdef" for c in config_hash):
            raise JobConflict("job requires an immutable configuration fingerprint")
        if not isinstance(resources, (tuple, list)) or len(resources) > 8 or any(not isinstance(r, str) or not 1 <= len(r) <= 256 for r in resources) or len(set(resources)) != len(resources):
            raise JobConflict("invalid job resource scope")
        deadline = expires_at if expires_at is not None else self.clock() + 900
        if not self.clock() < deadline <= self.clock() + 86400:
            raise JobConflict("invalid bounded queue authorization deadline")
        with self.transaction() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise JobConflict("job run not found")
            tenant, requester = run["tenant_id"] or "local", run["requested_by"] or "operator"
            counts = db.execute("SELECT COUNT(*) AS all_count, SUM(tenant_id=?) AS tenant_count, SUM(tenant_id=? AND requester=?) AS user_count "
                                "FROM jobs WHERE state IN ('queued','running')", (tenant, tenant, requester)).fetchone()
            if counts[0] >= self.max_queued or (counts[1] or 0) >= self.max_per_tenant or (counts[2] or 0) >= self.max_per_requester:
                raise JobConflict("durable job admission capacity exceeded")
            if db.execute("SELECT 1 FROM jobs WHERE run_id=? AND kind=?", (run_id, kind)).fetchone():
                raise JobConflict("this run phase was already submitted; duplicate dispatch denied")
            job_id = "job-" + uuid.uuid4().hex
            job = {"id": job_id, "run_id": run_id, "kind": kind, "state": "queued", "fence": 0}
            db.execute("INSERT INTO jobs(id,run_id,kind,state,payload,config_hash,tenant_id,requester,resources,created,expires_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (job_id, run_id, kind, "queued", canon(payload), config_hash, tenant, requester, canon(sorted(resources)), self.clock(), deadline))
            self._audit(job, "allow", "durable bounded job queued")
            return job_id

    def _recover(self, db):
        rows = db.execute("SELECT id,run_id,kind,state,owner,fence FROM jobs WHERE state='running' AND lease_until<=?", (self.clock(),)).fetchall()
        for row in rows:
            job = dict(row);job["state"] = "interrupted"
            db.execute("UPDATE jobs SET state='interrupted',error='worker lease expired; outcome uncertain; no automatic replay' WHERE id=?", (row["id"],))
            for resource in db.execute("SELECT resource FROM job_resources WHERE job_id=? AND owner=? AND fence=?", (row["id"], row["owner"], row["fence"])).fetchall():
                db.execute("INSERT OR IGNORE INTO job_quarantines VALUES(?,?,?,?)", (resource[0], row["id"], now(), "uncertain external effects; stop worker and reconcile target before release"))
            db.execute("DELETE FROM job_resources WHERE job_id=? AND owner=? AND fence=?", (row["id"], row["owner"], row["fence"]))
            db.execute("UPDATE runs SET status='interrupted',updated=?,error=? WHERE id=? AND status IN ('planning','executing','queued_planning','queued_execution','awaiting_approval','approved','completed')",
                       (now(), "worker lease expired; partial evidence preserved; new request/review required", row["run_id"]))
            self._audit(job, "deny", "expired worker fenced; uncertain work will not be replayed")
        expired = db.execute("SELECT id,run_id,kind,state,owner,fence FROM jobs WHERE state='queued' AND expires_at<=?", (self.clock(),)).fetchall()
        for row in expired:
            job = dict(row);job["state"] = "blocked"
            db.execute("UPDATE jobs SET state='blocked',error='queued authorization expired' WHERE id=?", (row["id"],))
            db.execute("UPDATE runs SET status='blocked',updated=?,error=? WHERE id=? AND status IN ('planning','executing','queued_planning','queued_execution')",
                       (now(), "queued request authorization expired; submit a new request", row["run_id"]))
            self._audit(job, "deny", "queued authorization expired before claim")
        return len(rows) + len(expired)

    def recover(self):
        with self.transaction() as db:
            return self._recover(db)

    def block_incompatible(self, config_hash):
        """A deployment may only dispatch its reviewed code/configuration revision."""
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM jobs WHERE state='queued' AND config_hash!=?", (config_hash,)).fetchall()
            for row in rows:
                reason = "queued code/configuration revision changed; new request and review required"
                db.execute("UPDATE jobs SET state='blocked',error=?,finished=? WHERE id=?", (reason, self.clock(), row["id"]))
                db.execute("UPDATE runs SET status='blocked',updated=?,error=? WHERE id=? AND status IN ('queued_planning','queued_execution')", (now(), reason, row["run_id"]))
                self._audit({**dict(row), "state": "blocked"}, "deny", reason)
            return len(rows)

    def claim(self, owner, config_hash, *, kinds=("planning", "execution")):
        if not isinstance(owner, str) or not 1 <= len(owner) <= 128 or not kinds or any(k not in self.running for k in kinds):
            raise JobConflict("invalid worker identity/kind")
        with self.transaction() as db:
            self._recover(db)
            candidates = db.execute("SELECT id,kind,resources,fence FROM jobs WHERE state='queued' AND config_hash=? ORDER BY created,id LIMIT ?", (config_hash, self.max_queued)).fetchall()
            for row in candidates:
                if row["kind"] not in kinds:
                    continue
                active = db.execute("SELECT COUNT(*) FROM jobs WHERE kind=? AND state='running'", (row["kind"],)).fetchone()[0]
                if active >= self.running[row["kind"]]:
                    continue
                resources = json.loads(row["resources"])
                if any(db.execute("SELECT 1 FROM job_quarantines WHERE resource=?", (resource,)).fetchone() for resource in resources):
                    continue
                if any(db.execute("SELECT 1 FROM job_resources WHERE resource=? AND expires>?", (resource, self.clock())).fetchone() for resource in resources):
                    continue
                row = db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
                fence, expiry = row["fence"] + 1, self.clock() + min(self.lease_s, self.max_job_s)
                db.execute("UPDATE jobs SET state='running',owner=?,fence=?,lease_until=?,started=?,attempts=attempts+1 WHERE id=? AND state='queued'",
                           (owner, fence, expiry, self.clock(), row["id"]))
                for resource in resources:
                    db.execute("INSERT INTO job_resources VALUES(?,?,?,?,?) ON CONFLICT(resource) DO UPDATE SET "
                               "job_id=excluded.job_id,owner=excluded.owner,fence=excluded.fence,expires=excluded.expires",
                               (resource, row["id"], owner, fence, expiry))
                job = {**dict(row), "state": "running", "owner": owner, "fence": fence, "lease_until": expiry,
                       "payload": json.loads(row["payload"]), "resources": resources, "started": self.clock()}
                self._audit(job, "allow", "worker claimed fenced job and resource leases")
                return job
        return None

    def assert_lease(self, job, *, allow_cancel=False):
        rows = self.store.q("SELECT * FROM jobs WHERE id=?", (job["id"],))
        if not rows or rows[0]["state"] != "running" or rows[0]["owner"] != job["owner"] or rows[0]["fence"] != job["fence"] or rows[0]["lease_until"] <= self.clock():
            raise LeaseLost("worker no longer owns a live fenced lease")
        if rows[0].get("started") is not None and self.clock() >= rows[0]["started"] + self.max_job_s:
            raise LeaseLost("job exceeded its bounded wall-clock lifetime")
        if rows[0]["cancel_requested"] and not allow_cancel:
            raise JobCancelled("job cancellation requested")
        return rows[0]

    def heartbeat(self, job):
        with self.transaction() as db:
            self.assert_lease(job)
            started = job.get("started") if job.get("started") is not None else self.clock()
            expiry = min(self.clock() + self.lease_s, started + self.max_job_s)
            db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND owner=? AND fence=?", (expiry, job["id"], job["owner"], job["fence"]))
            db.execute("UPDATE job_resources SET expires=? WHERE job_id=? AND owner=? AND fence=?", (expiry, job["id"], job["owner"], job["fence"]))
            return expiry

    def abandon(self, job):
        """Fence an owned job on shutdown/lost authority without replaying its effects."""
        with self.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND state='running' AND owner=? AND fence=?",
                       (self.clock(), job["id"], job["owner"], job["fence"]))
            return self._recover(db)

    def finish(self, job, state="completed", error=None):
        if state not in ("completed", "failed", "blocked", "cancelled"):
            raise JobConflict("invalid terminal job state")
        if error is not None:
            error = redact(str(error))[:2000]
        with self.transaction() as db:
            try:
                self.assert_lease(job)
            except JobCancelled:
                state = "cancelled"
            db.execute("UPDATE jobs SET state=?,error=?,finished=? WHERE id=? AND owner=? AND fence=?",
                       (state, error, self.clock(), job["id"], job["owner"], job["fence"]))
            db.execute("DELETE FROM job_resources WHERE job_id=? AND owner=? AND fence=?", (job["id"], job["owner"], job["fence"]))
            if state == "cancelled":
                db.execute("UPDATE runs SET status='cancelled',updated=? WHERE id=? AND status IN ('planning','executing','approved','queued_planning','queued_execution')", (now(), job["run_id"]))
            self._audit({**job, "state": state}, "allow", "fenced job completed without automatic replay")

    def cancel(self, run_id):
        with self.transaction() as db:
            run = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] in ("completed", "failed", "blocked", "cancelled", "interrupted", "rejected", "analysis_only"):
                raise JobConflict("run is missing or already terminal")
            rows = db.execute("SELECT * FROM jobs WHERE run_id=? AND state IN ('queued','running')", (run_id,)).fetchall()
            for row in rows:
                state = "cancelled" if row["state"] == "queued" else "running"
                db.execute("UPDATE jobs SET state=?,cancel_requested=1 WHERE id=?", (state, row["id"]))
                self._audit({**dict(row), "state": state}, "allow", "durable job cancellation recorded")
            db.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
            if not any(row["state"] == "running" for row in rows):
                db.execute("UPDATE runs SET status='cancelled',updated=? WHERE id=?", (now(), run_id))
            return len(rows)

    def reconcile_resource(self, resource, reviewer, reason, *, confirmed_stopped=False):
        """Operator-only release after stopping the old worker and reconciling SUT effects.

        This does not retry the interrupted job or certify its partial results.
        No web endpoint, model tool or automatic recovery calls this operation.
        """
        if confirmed_stopped is not True or not isinstance(reviewer, str) or not 1 <= len(reviewer) <= 200 or not isinstance(reason, str) or not 10 <= len(reason) <= 1000:
            raise JobConflict("explicit operator identity, reconciliation reason and stopped-worker confirmation required")
        with self.transaction() as db:
            row = db.execute("SELECT q.*,j.run_id,j.kind,j.fence,j.state FROM job_quarantines q JOIN jobs j ON j.id=q.job_id WHERE q.resource=?", (resource,)).fetchone()
            if not row:
                raise JobConflict("resource quarantine not found")
            if db.execute("SELECT 1 FROM job_resources WHERE resource=? AND expires>?", (resource, self.clock())).fetchone():
                raise JobConflict("resource still has a live worker lease")
            args = {"resource": resource, "job_id": row["job_id"], "reviewer": reviewer, "reconciliation": reason}
            self.store.append_audit({"ts": now(), "run_id": row["run_id"], "tool": "job.resource_reconciled", "args_hash": sha256(canon(args)),
                                     "args_summary": redact(canon(args)), "decision": "allow", "reason": "operator attested stopped worker and reconciled external effects",
                                     "outcome": "resource quarantine released; interrupted job not retried", "duration_ms": 0})
            db.execute("DELETE FROM job_quarantines WHERE resource=?", (resource,))
