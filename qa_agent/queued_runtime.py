"""Fixed runtime phases over the durable queue. Enabled explicitly during rollout."""
from . import ROOT
from .jobs import DurableJobs, JobConflict
from .workers import JobWorkers, JobAccessDenied
from .util import canon, sha256


class QueuedRuntime:
    def __init__(self, agent):
        self.agent = agent
        self.jobs = DurableJobs(agent.store, agent.cfg.get("jobs"))
        self.configuration = sha256(canon({"config": agent.cfg, "policy": agent.policy}))
        implementation = {str(path.relative_to(ROOT)): sha256(path.read_bytes()) for path in sorted((ROOT / "qa_agent").rglob("*.py"))}
        from .dependencies import fingerprint
        self.fingerprint = sha256(canon({"configuration": self.configuration, "implementation": implementation, "dependencies": fingerprint()}))
        self.authorizer = None
        self.workers = JobWorkers(self.jobs, self.fingerprint, self._dispatch, authorize=self._authorize, after_job=self._finished)

    def _authorize(self, job):
        if sha256(canon({"config": self.agent.cfg, "policy": self.agent.policy})) != self.configuration:
            raise JobAccessDenied("worker configuration changed; submit a new request")
        run = self.agent.store.get_run(job["run_id"])
        if not run or self.agent.connections.revision(run.get("project")) != job["payload"]["connection_scopes"]:
            raise JobAccessDenied("queued connection/project scope changed")
        actor = job["payload"]["authorization"]
        if actor.get("mode") == "operator":
            return True  # Trusted in-process/CLI call; never accepted from an HTTP body.
        if self.authorizer is None:
            raise JobAccessDenied("workspace caller verification is unavailable")
        return self.authorizer(job)

    def _payload(self, run_id, authorization=None, **fields):
        run = self.agent.store.get_run(run_id)
        return {"authorization": authorization or {"mode": "operator"},
                "connection_scopes": self.agent.connections.revision(run.get("project")), **fields}

    def _kick(self, run_id, job_id, wait):
        self.agent.gateway.audit_delivery.flush()
        try:
            self.workers.wake()
        except (RuntimeError, JobConflict):
            self.agent.store.event(run_id, "queue", "Work accepted durably; local worker unavailable; pending dispatch retained.")
        if wait:
            self.workers.wait(job_id)
        return run_id

    def start(self, run_id, create_run, story_key, environment, authorization=None, wait=False):
        with self.jobs.transaction():
            if self.workers.closing.is_set():
                raise JobConflict("worker pool is stopping; admission unavailable")
            create_run()
            self.agent.store.update_run(run_id, status="queued_planning", story_key=story_key)
            job_id = self.jobs.enqueue(run_id, "planning", self._payload(run_id, authorization, story_key=story_key, environment=environment), self.fingerprint)
        return self._kick(run_id, job_id, wait)

    def _current(self, run_id, expected_status, plan_hash):
        from .runtime import ConflictError
        run = self.agent.store.get_run(run_id)
        if not run or run["status"] != expected_status or run["plan_hash"] != plan_hash or run.get("cancel_requested"):
            raise ConflictError("run state or reviewed plan changed before submission")
        if self.agent._plan_expired(run):
            raise ConflictError("reviewed plan expired before submission")
        return run

    def approve(self, run, approver, decision, comment, identity, auth_mode, approver_label, authorization, wait):
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved|rejected")
        with self.jobs.transaction():
            if self.workers.closing.is_set():
                raise JobConflict("worker pool is stopping; admission unavailable")
            current = self._current(run["id"], "awaiting_approval", run["plan_hash"])
            self.agent.approvals.record(run["id"], current["plan_hash"], approver, decision, comment,
                                        identity=identity, auth_mode=auth_mode, approver_label=approver_label)
            self.agent.store.event(run["id"], "approval", f"Plan {decision}; exact envelope {current['plan_hash']}.")
            if decision == "rejected":
                self.agent.store.update_run(run["id"], status="rejected")
                job_id = None
            else:
                self.agent.store.update_run(run["id"], status="queued_execution")
                job_id = self.jobs.enqueue(run["id"], "execution", self._payload(run["id"], authorization), self.fingerprint,
                                           resources=["execution-global"])
                self.agent.gateway.record(run["id"], "plan.approve", {"plan_hash": current["plan_hash"], "identity": identity},
                                          "allow", "approval and execution queue committed atomically")
        if job_id is None:
            self.agent.gateway.audit_delivery.flush()
            return run["id"]
        return self._kick(run["id"], job_id, wait)

    def record_review(self, run, principal, decision, plan_hash, comment):
        with self.jobs.transaction():
            self._current(run["id"], "awaiting_approval", plan_hash)
            self.agent.approvals.record(run["id"], plan_hash, principal.name, decision, comment, identity=principal.name, auth_mode=principal.mode)
            self.agent.store.update_run(run["id"], status="approved" if decision == "approved" else "rejected")
            self.agent.gateway.record(run["id"], "plan.review", {"user": principal.name, "plan_hash": plan_hash, "decision": decision},
                                      "allow", "authenticated manual review committed; execution deferred")
        self.agent.gateway.audit_delivery.flush()
        return {"run_id": run["id"], "decision": decision, "plan_hash": plan_hash, "approver": principal.name}

    def execute(self, run_id, *, approved=False, authorization=None, wait=True):
        from .runtime import ConflictError
        with self.jobs.transaction():
            if self.workers.closing.is_set():
                raise JobConflict("worker pool is stopping; admission unavailable")
            run = self.agent.store.get_run(run_id)
            if not run:
                raise ConflictError("run not found")
            if approved:
                self._current(run_id, "approved", run["plan_hash"])
                latest = self.agent.approvals.latest(run_id)
                if not latest or not latest.get("identity") or not self.agent.approvals.is_approved(run_id, run["plan_hash"]):
                    raise ConflictError("run has no verified exact-plan approval")
            self.agent.store.update_run(run_id, status="queued_execution")
            job_id = self.jobs.enqueue(run_id, "execution", self._payload(run_id, authorization), self.fingerprint,
                                       resources=["execution-global"])
        return self._kick(run_id, job_id, wait)

    def cancel(self, run_id, by):
        with self.jobs.transaction():
            self.jobs.cancel(run_id)
            self.agent.store.event(run_id, "cancel", f"Durable cancellation requested by {by}.")
        self.agent.gateway.audit_delivery.flush()
        return run_id

    def _dispatch(self, job):
        run = self.agent.store.get_run(job["run_id"])
        if job["kind"] == "planning":
            if run["status"] != "queued_planning":
                raise JobAccessDenied("queued planning state changed")
            if run["id"] not in self.agent._run_models:
                self.agent._run_models[run["id"]] = self.agent.models.resolve(run["model_provider"], run.get("model_id"))
            self.agent.store.update_run(run["id"], status="planning")
            self.agent._plan(run["id"], job["payload"]["story_key"], job["payload"]["environment"])
        elif job["kind"] == "execution":
            if run["status"] != "queued_execution":
                raise JobAccessDenied("queued execution state changed")
            self.agent.store.update_run(run["id"], status="executing")
            self.agent._execute(run["id"])
        else:
            raise JobAccessDenied("unsupported queued runtime phase")

    def _finished(self, job):
        self.agent._run_models.pop(job["run_id"], None)
        self.agent.gateway.audit_delivery.flush()

    def recover_pending(self):
        if self.agent.review_only:
            raise JobConflict("review-only runtime cannot start workers")
        self.jobs.recover()
        self.jobs.block_incompatible(self.fingerprint)
        self.agent.gateway.audit_delivery.flush()
        self.workers.wake()

    def close(self, timeout=5):
        return self.workers.close(timeout)
