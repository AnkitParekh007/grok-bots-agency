import json, threading
import pytest
from qa_agent.executor import PlaywrightExecutor
from qa_agent.runtime import QAAgent, ConflictError
from qa_agent.store import Store
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.execution, pytest.mark.integration]

PROMPT = "Read the synthetic supplier import story, generate test cases, execute the approved tests, and give me the results."


def ready(tmp_path, **cfg):
    a = QAAgent(tmp_path, agent_cfg=agent_cfg("replay", **cfg) if cfg else None)
    rid = a.start_run(PROMPT, wait=True)
    return a, rid


def test_single_active_execution_lock(tmp_path):
    a, rid = ready(tmp_path)
    a.exec_lock.acquire()
    try:
        with pytest.raises(ConflictError, match="another execution"):
            a.approve(rid, "x", "approved")
        assert a.approvals.latest(rid) is None and a.store.get_run(rid)["status"] == "awaiting_approval"
    finally:
        a.exec_lock.release()


def test_cancel_while_awaiting_approval(tmp_path):
    a, rid = ready(tmp_path)
    a.cancel(rid, by="t")
    assert a.store.get_run(rid)["status"] == "cancelled"
    with pytest.raises(ConflictError):
        a.approve(rid, "x", "approved")
    with pytest.raises(ConflictError):
        a.cancel(rid)


def test_controlled_cancellation_mid_execution(tmp_path, monkeypatch, sut_factory):
    with sut_factory("all") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        a, rid = ready(tmp_path)
        orig = PlaywrightExecutor._run_one

        def run_one(self, *args, **kw):
            res = orig(self, *args, **kw)
            if res["id"] == "TC-02":
                a.cancel(rid, by="tester")
            return res
        monkeypatch.setattr(PlaywrightExecutor, "_run_one", run_one)
        a.approve(rid, "x", "approved", wait=True)
        run = a.store.get_run(rid)
        st = {r["id"]: r["status"] for r in a.store.results(rid)}
        assert run["status"] == "cancelled", run["error"]
        assert st["TC-01"] == "passed" and st["TC-02"] == "failed"
        assert all(st[f"TC-0{i}"] == "not_run" for i in range(3, 9))
        rep = json.loads(a.artifacts.path_for(rid, "report.json").read_text())
        assert rep["not_run"] == 6 and rep["run_status_at_report"] == "cancelled" and rep["per_ac"]["AC5"] == "not_run"
        assert not a.exec_lock.locked()


def test_interrupted_runs_marked_on_restart(tmp_path):
    s = Store(tmp_path)
    s.create_run("run-x", "r", "synthetic-local", "replay", "L")
    s.update_run("run-x", status="executing")
    s.db.close()
    s2 = Store(tmp_path)
    r = s2.get_run("run-x")
    assert r["status"] == "interrupted" and "re-run required" in r["error"]
    assert s2.events("run-x")[-1]["step"] == "interrupted"


def test_timeouts_produce_error_never_pass_with_evidence(tmp_path, monkeypatch, sut_factory):
    with sut_factory("slow_api") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        a, rid = ready(tmp_path, execution={"api_timeout_ms": 1000, "action_timeout_ms": 5000, "navigation_timeout_ms": 5000})
        a.approve(rid, "x", "approved", wait=True)
        res = {r["id"]: r for r in a.store.results(rid)}
        slow = ["TC-02", "TC-03", "TC-04", "TC-05"]          # api_product_after_import hits the slow endpoint
        for t in slow:
            assert res[t]["status"] == "error" and "Timeout" in res[t]["error"], res[t]
            for k in ("screenshot", "trace", "log"):
                assert a.artifacts.path_for(rid, res[t][k]).stat().st_size > 100
        assert all(res[t]["status"] == "passed" for t in ("TC-01", "TC-06", "TC-07", "TC-08"))
        run = a.store.get_run(rid)
        assert run["summary_json"] == {"total": 8, "passed": 4, "failed": 0, "error": 4, "blocked": 0}
        rep = json.loads(a.artifacts.path_for(rid, "report.json").read_text())
        assert rep["per_ac"]["AC2"] == "error" and rep["per_ac"]["AC3"] == "error"


def test_replay_is_cost_free(tmp_path):
    a, rid = ready(tmp_path)
    u = a.store.run_usage(rid)["totals"]
    assert u["calls"] == 1 and u["cost_usd"] == 0 and u["total_tokens"] == 0
