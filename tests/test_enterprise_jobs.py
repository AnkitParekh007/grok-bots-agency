"""Durable queue contracts; runtime/worker wiring is validated separately."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from qa_agent.jobs import DurableJobs, JobConflict, LeaseLost, JobCancelled
from qa_agent.store import Store

pytestmark = [pytest.mark.execution, pytest.mark.security, pytest.mark.audit]
CONFIG = "a" * 64


@pytest.fixture
def setup(tmp_path):
    clock = [1000.0]
    store = Store(tmp_path)
    jobs = DurableJobs(store, clock=lambda: clock[0])
    def run(rid, who="requester", tenant="tenant-a"):
        store.create_run(rid, "synthetic queue contract", "synthetic-local", "replay", "test")
        store.update_run(rid, tenant_id=tenant, requested_by=who)
        return rid
    yield store, jobs, clock, run
    store.db.close()


def test_queued_work_survives_another_store_open_and_duplicate_is_denied(setup):
    store, jobs, _, run = setup
    rid = run("queued")
    job_id = jobs.enqueue(rid, "planning", {"story_key": "PCC-1"}, CONFIG)
    other = Store(store.data_dir)
    try:
        assert other.get_run(rid)["status"] == "planning"
        assert other.q("SELECT state FROM jobs WHERE id=?", (job_id,))[0]["state"] == "queued"
        with pytest.raises(JobConflict, match="duplicate"):
            jobs.enqueue(rid, "planning", {}, CONFIG)
    finally:
        other.db.close()


def test_only_one_connection_claims_a_job_and_resource(setup):
    store, jobs, _, run = setup
    jobs = DurableJobs(store, {"execution_workers": 2}, clock=jobs.clock)
    jobs.enqueue(run("one"), "execution", {}, CONFIG, resources=["synthetic-target"])
    jobs.enqueue(run("two"), "execution", {}, CONFIG, resources=["synthetic-target"])
    other = Store(store.data_dir)
    second = DurableJobs(other, {"execution_workers": 2}, clock=jobs.clock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda item: item[0].claim(item[1], CONFIG), [(jobs, "worker-one"), (second, "worker-two")]))
        live = [job for job in claimed if job]
        assert len(live) == 1
        jobs.finish(live[0])
        next_job = second.claim("worker-two", CONFIG)
        assert next_job and next_job["id"] != live[0]["id"]
    finally:
        other.db.close()


def test_expired_worker_is_fenced_and_execution_is_not_replayed(setup):
    store, jobs, clock, run = setup
    rid = run("uncertain")
    store.update_run(rid, status="executing")
    jobs.enqueue(rid, "execution", {}, CONFIG, resources=["synthetic-target"])
    claimed = jobs.claim("stale-worker", CONFIG)
    clock[0] += 61
    assert jobs.recover() == 1
    assert store.get_run(rid)["status"] == "interrupted"
    assert jobs.claim("new-worker", CONFIG) is None
    with pytest.raises(LeaseLost): jobs.assert_lease(claimed)
    with pytest.raises(LeaseLost): jobs.heartbeat(claimed)
    with pytest.raises(LeaseLost): jobs.finish(claimed)
    assert store.q("SELECT * FROM job_resources") == []


def test_heartbeat_keeps_lease_live_and_stale_fence_cannot_finish(setup):
    _, jobs, clock, run = setup
    jobs.enqueue(run("heartbeat"), "planning", {}, CONFIG)
    job = jobs.claim("worker", CONFIG)
    clock[0] += 30
    assert jobs.heartbeat(job) == clock[0] + 60
    clock[0] += 31
    assert jobs.assert_lease(job)
    with pytest.raises(LeaseLost): jobs.finish({**job, "fence": job["fence"] - 1})
    jobs.finish(job)


def test_queued_expiry_and_configuration_mismatch_cannot_dispatch(setup):
    store, jobs, clock, run = setup
    jobs.enqueue(run("expires"), "planning", {}, CONFIG, expires_at=clock[0] + 5)
    assert jobs.claim("worker", "b" * 64) is None
    clock[0] += 6
    assert jobs.claim("worker", CONFIG) is None
    assert store.get_run("expires")["status"] == "blocked"


def test_per_requester_and_tenant_admission_are_atomic(setup):
    store, _, clock, run = setup
    jobs = DurableJobs(store, {"max_per_requester": 1, "max_per_tenant": 2}, clock=lambda: clock[0])
    jobs.enqueue(run("first"), "planning", {}, CONFIG)
    with pytest.raises(JobConflict, match="capacity"):
        jobs.enqueue(run("same-user"), "planning", {}, CONFIG)
    jobs.enqueue(run("another", "other"), "planning", {}, CONFIG)
    with pytest.raises(JobConflict, match="capacity"):
        jobs.enqueue(run("same-tenant", "third"), "planning", {}, CONFIG)
    assert len(store.q("SELECT * FROM jobs")) == 2


def test_cancelled_queued_work_never_claims_and_running_work_is_cooperative(setup):
    store, jobs, _, run = setup
    jobs.enqueue(run("queued-cancel"), "planning", {}, CONFIG)
    jobs.cancel("queued-cancel")
    assert store.get_run("queued-cancel")["status"] == "cancelled"
    assert jobs.claim("worker", CONFIG) is None
    jobs.enqueue(run("running-cancel"), "planning", {}, CONFIG)
    live = jobs.claim("worker", CONFIG)
    jobs.cancel("running-cancel")
    with pytest.raises(JobCancelled): jobs.assert_lease(live)
    jobs.finish(live)
    assert store.q("SELECT state FROM jobs WHERE id=?", (live["id"],))[0]["state"] == "cancelled"


def test_job_transition_and_audit_outbox_commit_together(setup):
    store, jobs, _, run = setup
    jobs.enqueue(run("atomic-audit"), "planning", {}, CONFIG)
    assert store.verify_audit_chain()["ok"]
    rows = store.q("SELECT * FROM audit WHERE tool='job.transition'")
    assert len(rows) == 1 and rows[0]["outcome"] == "queued"
    assert len(store.q("SELECT * FROM audit_outbox WHERE seq=?", (rows[0]["seq"],))) == 1


def test_fenced_context_refuses_stale_state_results_and_artifact_paths(setup, tmp_path):
    from qa_agent.store import FileArtifactStore
    store, jobs, clock, run = setup
    rid = run("stale-writes")
    jobs.enqueue(rid, "planning", {}, CONFIG)
    claimed = jobs.claim("worker", CONFIG)
    artifacts = FileArtifactStore(tmp_path / "evidence", guard=store.assert_job_lease)
    with store.fenced_job(jobs, claimed):
        store.event(rid, "owned", "lease is active")
        clock[0] += 61
        with pytest.raises(LeaseLost): store.update_run(rid, status="completed")
        with pytest.raises(LeaseLost): store.save_result(rid, {"id": "TC-1", "status": "passed"})
        with pytest.raises(LeaseLost): artifacts.write_bytes(rid, "false-success.json", b'{}')
        with pytest.raises(LeaseLost): store.event("different-run", "bad", "foreign scope")
    assert store.results(rid) == []


def test_enqueue_and_run_creation_can_commit_or_roll_back_as_one_transaction(setup):
    store, jobs, _, run = setup
    with pytest.raises(RuntimeError):
        with jobs.transaction():
            jobs.enqueue(run("rollback"), "planning", {}, CONFIG)
            raise RuntimeError("synthetic admission failure")
    assert store.get_run("rollback") is None
    assert store.q("SELECT * FROM jobs") == []
    assert store.q("SELECT * FROM audit WHERE tool='job.transition'") == []


def test_bounded_pool_drains_persisted_jobs_without_per_job_threads(setup):
    from qa_agent.workers import JobWorkers
    store, jobs, _, run = setup
    ids = [jobs.enqueue(run(f"batch-{i}"), "planning", {}, CONFIG) for i in range(10)]
    lock, active, peak = threading.Lock(), [0], [0]
    def dispatch(job):
        with lock:
            active[0] += 1;peak[0] = max(peak[0], active[0])
        try:
            time.sleep(0.02)
            store.update_run(job["run_id"], status="completed")
        finally:
            with lock: active[0] -= 1
    workers = JobWorkers(jobs, CONFIG, dispatch)
    try:
        workers.wake()
        assert all(workers.wait(job_id, timeout=10) == "completed" for job_id in ids)
        assert 1 <= peak[0] <= jobs.running["planning"]
        assert len(workers.threads) <= workers.limit
    finally:
        assert workers.close()


def test_pool_revalidates_actor_before_dispatch(setup):
    from qa_agent.workers import JobWorkers
    store, jobs, _, run = setup
    job_id = jobs.enqueue(run("revoked-actor"), "planning", {}, CONFIG)
    called = []
    workers = JobWorkers(jobs, CONFIG, lambda job: called.append(job), authorize=lambda job: False)
    try:
        assert workers.wait(job_id, timeout=5) == "blocked"
        assert not called and store.get_run("revoked-actor")["status"] == "blocked"
    finally:
        workers.close()


def test_expired_inflight_worker_cannot_commit_success_after_recovery(setup):
    from qa_agent.workers import JobWorkers
    store, jobs, clock, run = setup
    job_id = jobs.enqueue(run("inflight"), "planning", {}, CONFIG)
    entered, release = threading.Event(), threading.Event()
    def dispatch(job):
        entered.set()
        assert release.wait(5)
        store.update_run(job["run_id"], status="completed")
    workers = JobWorkers(jobs, CONFIG, dispatch)
    try:
        workers.wake();assert entered.wait(5)
        clock[0] += 61
        jobs.recover()
        release.set()
        assert workers.wait(job_id, timeout=5) == "interrupted"
        assert workers.close()
        assert store.get_run("inflight")["status"] == "interrupted"
    finally:
        release.set();workers.close()


def test_captured_guard_remains_effective_in_helper_thread_without_thread_local_state(setup):
    store, jobs, clock, run = setup
    jobs.enqueue(run("proxy-fence"), "execution", {}, CONFIG)
    job = jobs.claim("worker", CONFIG)
    with store.fenced_job(jobs, job):
        guard = store.capture_job_guard()
    clock[0] += 61
    caught = []
    def helper():
        try: guard()
        except LeaseLost: caught.append(True)
    thread = threading.Thread(target=helper)
    thread.start();thread.join(5)
    assert caught == [True]


def test_expired_execution_quarantines_target_until_operator_reconciles_effects(setup):
    store, jobs, clock, run = setup
    jobs.enqueue(run("uncertain-target"), "execution", {}, CONFIG, resources=["execution-global"])
    clock[0] += 1  # Distinct enqueue times; equal times deliberately use UUID order.
    jobs.enqueue(run("waiting-target"), "execution", {}, CONFIG, resources=["execution-global"])
    original = jobs.claim("old-worker", CONFIG)
    clock[0] += 61
    assert jobs.claim("new-worker", CONFIG) is None
    quarantines = store.q("SELECT * FROM job_quarantines")
    assert len(quarantines) == 1 and quarantines[0]["job_id"] == original["id"]
    with pytest.raises(JobConflict, match="confirmation"):
        jobs.reconcile_resource("execution-global", "operator", "Target was inspected")
    jobs.reconcile_resource("execution-global", "operator", "Stopped original worker, inspected target and removed synthetic partial import", confirmed_stopped=True)
    next_job = jobs.claim("new-worker", CONFIG)
    assert next_job and next_job["run_id"] == "waiting-target"
    assert store.q("SELECT state FROM jobs WHERE id=?", (original["id"],))[0]["state"] == "interrupted"
    assert store.q("SELECT * FROM audit WHERE tool='job.resource_reconciled'")
