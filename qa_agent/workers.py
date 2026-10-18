"""On-demand bounded worker pool for the durable queue; no payload-supplied callables."""
import threading
import time
import uuid

from .jobs import JobConflict, JobCancelled, LeaseLost
from .util import redact


class JobAccessDenied(JobConflict):
    pass


class JobWorkers:
    def __init__(self, jobs, config_hash, dispatch, *, authorize=None, after_job=None):
        self.jobs, self.store, self.config_hash, self.dispatch = jobs, jobs.store, config_hash, dispatch
        self.authorize, self.after_job = authorize, after_job
        self.limit = sum(jobs.running.values())
        self.threads, self.active = [], {}
        self.lock, self.closing = threading.RLock(), threading.Event()

    def _validate(self, job):
        if self.closing.is_set():
            raise LeaseLost("worker is stopping; no further tool dispatch")
        if self.authorize:
            if self.authorize(job) is False:
                raise JobAccessDenied("queued caller authorization is no longer valid")

    def wake(self):
        if self.closing.is_set():
            raise JobConflict("worker pool is stopping")
        with self.lock:
            self.threads[:] = [thread for thread in self.threads if thread.is_alive()]
            pending = self.store.q("SELECT COUNT(*) AS n FROM jobs WHERE config_hash=? AND state='queued'", (self.config_hash,))[0]["n"]
            target = min(self.limit, pending + len(self.active))
            while len(self.threads) < target:
                thread = threading.Thread(target=self._loop, name="qa-job-worker", daemon=True)
                self.threads.append(thread)
                thread.start()

    def wait(self, job_id, timeout=None):
        started = time.monotonic()
        while True:
            row = self.store.q("SELECT state FROM jobs WHERE id=?", (job_id,))
            if not row:
                raise JobConflict("queued job not found")
            if row[0]["state"] not in ("queued", "running"):
                return row[0]["state"]
            if timeout is not None and time.monotonic() - started > timeout:
                raise TimeoutError("job wait deadline exceeded; durable work remains tracked")
            self.wake()
            self.closing.wait(0.05)

    def _loop(self):
        normal_exit = False
        try:
            self._work()
            normal_exit = True
        finally:
            with self.lock:
                current = threading.current_thread()
                if current in self.threads:
                    self.threads.remove(current)
                # Enqueue may race a worker's decision to exit. Recheck while
                # holding the same lock used by wake's live-worker accounting.
                if normal_exit and not self.closing.is_set():
                    self.wake()

    def _work(self):
        owner = "worker-" + uuid.uuid4().hex
        while not self.closing.is_set():
            job = self.jobs.claim(owner, self.config_hash)
            if job is None:
                pending = self.store.q("SELECT COUNT(*) AS n FROM jobs WHERE state='queued' AND config_hash=?", (self.config_hash,))[0]["n"]
                if not pending:
                    return
                self.closing.wait(0.1)
                continue
            with self.lock:
                self.active[job["id"]] = job
            stop = threading.Event()
            def renew(job_ref=job, stop_event=stop):
                while not stop_event.wait(min(5, self.jobs.lease_s / 3)):
                    try:
                        self.jobs.assert_lease(job_ref)
                        if self.closing.is_set():
                            return
                        self.jobs.heartbeat(job_ref)
                    except JobConflict:
                        return
            renewer = threading.Thread(target=renew, name="qa-job-heartbeat", daemon=True)
            renewer.start()
            state, error = "completed", None
            try:
                with self.store.fenced_job(self.jobs, job, self._validate):
                    try:
                        self.jobs.assert_lease(job)
                        self._validate(job)
                        self.dispatch(job)
                        self._validate(job)
                        self.jobs.assert_lease(job)
                        run = self.store.get_run(job["run_id"])
                        if run and run["status"] in ("failed", "blocked", "cancelled"):
                            state, error = run["status"], run.get("error")
                    except JobCancelled:
                        state = "cancelled"
                        self.store.update_run(job["run_id"], status="cancelled")
                    except LeaseLost:
                        raise
                    except Exception as exc:
                        state = "blocked" if isinstance(exc, JobAccessDenied) else "failed"
                        error = redact(f"{type(exc).__name__}: {exc}")[:2000]
                        self.store.update_run(job["run_id"], status=state, error=error)
                        self.store.event(job["run_id"], "error", error)
                self.jobs.finish(job, state, error)
            except LeaseLost:
                # Recovery owns terminal state; stale worker writes are forbidden.
                self.jobs.abandon(job)
            finally:
                stop.set();renewer.join(1)
                with self.lock:
                    self.active.pop(job["id"], None)
                if self.after_job:
                    try:
                        self.after_job(job)
                    except Exception as exc:
                        # Business result is already durable; projection failures
                        # remain in the outbox and must not strand other jobs.
                        import logging
                        logging.getLogger(__name__).error("job finalization callback failed: %s", type(exc).__name__)

    def close(self, timeout=5):
        self.closing.set()
        with self.lock:
            active = list(self.active.values())
        for job in active:
            self.jobs.abandon(job)
        deadline = time.monotonic() + timeout
        for thread in list(self.threads):
            thread.join(max(0, deadline - time.monotonic()))
        # In-flight external calls cannot be undone. Their next fence check fails;
        # lease recovery must retain an uncertain outcome rather than replay them.
        return not any(thread.is_alive() for thread in self.threads)
