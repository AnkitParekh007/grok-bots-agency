"""Durable queue contracts; runtime/worker wiring is validated separately."""
from concurrent.futures import ThreadPoolExecutor

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
