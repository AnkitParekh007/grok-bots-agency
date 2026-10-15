"""Regression tests for PR #2 review findings. No live model calls."""
import copy, json, socket, threading
from datetime import datetime, timedelta, timezone
import httpx, pytest
from qa_agent import ROOT
from qa_agent.providers.models import OpenAICompatibleProvider, ProviderConfigError
from qa_agent.runtime import QAAgent, _BudgetLedger, BudgetExceeded
from qa_agent.store import Store
from qa_agent.util import load_json
from tests.b1_helpers import compatible_cfg, agent_cfg, fake_key

CTX = {"story_key": "PCC-DEMO-2", "data_class": "synthetic", "story": load_json(ROOT / "fixtures/jira/PCC-DEMO-2.json"), "catalog": {}}


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    real = socket.socket.connect
    def guard(self, addr):
        if isinstance(addr, tuple) and addr[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"external network attempted: {addr}")
        return real(self, addr)
    monkeypatch.setattr(socket.socket, "connect", guard)


@pytest.fixture
def key(monkeypatch):
    k = fake_key(); monkeypatch.setenv("MODEL_API_KEY", k); return k


# ---------------- (5) budget readiness ----------------
@pytest.mark.cost
def test_repo_budget_defaults_are_proposed_eval_caps():
    b = load_json(ROOT / "config/agent.json")["budget"]
    assert (b["per_run_usd"], b["daily_usd"], b["total_usd"]) == (1.0, 15.0, 15.0) and b["scope"]
    g = load_json(ROOT / "config/agent.json")["providers"]["compatible"]
    assert g["enabled"] is False and g["approved"] is False and all(v is None for v in g["pricing"].values() if not isinstance(v, str))


@pytest.mark.cost
@pytest.mark.parametrize("pricing", [
    {"input_per_mtok_usd": None, "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 6.0},
    {"input_per_mtok_usd": -1.0, "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 6.0},
    {"input_per_mtok_usd": 2.0, "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 0},
    {"input_per_mtok_usd": "2", "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 6.0},
    {"input_per_mtok_usd": True, "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 6.0},
    {"input_per_mtok_usd": 2.0, "output_per_mtok_usd": 6.0}])
def test_pricing_validation_fails_closed(key, pricing):
    with pytest.raises(ProviderConfigError, match="pricing"):
        OpenAICompatibleProvider(compatible_cfg("http://127.0.0.1:9/v1", pricing=pricing))


@pytest.mark.cost
@pytest.mark.parametrize("bad", [{"total_usd": None}, {"total_usd": 0}, {"daily_usd": -1}, {"per_run_usd": "1"}, {"scope": ""}, {"scope": None}])
def test_billable_provider_requires_all_caps(tmp_path, key, bad):
    cfg = agent_cfg("compatible", "http://127.0.0.1:9/v1"); cfg["budget"].update(bad)
    with pytest.raises(ProviderConfigError, match="budget"):
        QAAgent(tmp_path, agent_cfg=cfg)


def stub_reserve(st, run, est, day=None, scope="s1", total=15.0, per_run=1.0, daily=15.0):
    uid, reason = st.reserve_spend(run, "compatible", "m", "plan_story", 0, 0, est, per_run, daily, total, scope)
    if uid and day:
        st.x("UPDATE usage SET day=? WHERE id=?", (day, uid))
    return uid, reason


@pytest.mark.cost
def test_total_cap_persists_across_runs_days_and_restarts(tmp_path):
    st = Store(tmp_path)
    for i in range(14):                                   # 14 runs x $1 on 14 different days (daily cap never hit)
        assert stub_reserve(st, f"r{i}", 1.0, day=f"2026-09-{i + 1:02d}")[0]
    st2 = Store(tmp_path)                                 # "restart": new connection, same DB
    assert stub_reserve(st2, "r14", 1.0, day="2026-09-20")[0]
    uid, reason = stub_reserve(st2, "r15", 0.5)
    assert uid is None and "total cap $15.0 for scope 's1'" in reason
    assert stub_reserve(st2, "r16", 0.5, scope="other-scope")[0]   # other scopes unaffected
    assert st2.scope_spend("s1") == pytest.approx(15.0)


@pytest.mark.cost
def test_total_cap_atomic_under_concurrency(tmp_path):
    Store(tmp_path)
    stores, ok, start = [Store(tmp_path) for _ in range(20)], [], threading.Barrier(20)
    def w(i):
        start.wait()
        if stub_reserve(stores[i], f"r{i}", 1.0, day=f"2026-08-{i + 1:02d}", per_run=5, daily=100)[0]:
            ok.append(i)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(20)]; [t.start() for t in ts]; [t.join() for t in ts]
    assert len(ok) == 15 and Store(tmp_path).scope_spend("s1") == pytest.approx(15.0)


@pytest.mark.cost
def test_total_cap_enforced_through_provider_ledger(tmp_path, key):
    cfg = agent_cfg("compatible", "http://127.0.0.1:9/v1"); cfg["budget"].update(total_usd=1e-9, scope="tiny")
    a = QAAgent(tmp_path, agent_cfg=cfg)
    calls = []
    p = OpenAICompatibleProvider(compatible_cfg("http://127.0.0.1:9/v1"), gateway=a.gateway, transport=httpx.MockTransport(lambda r: calls.append(r)), sleep=lambda x: None)
    with pytest.raises(BudgetExceeded, match="total cap"):
        p.generate("plan_story", CTX, {}, run_id="r", budget=_BudgetLedger(a, "r", "compatible", "m", "plan_story", 0))
    assert calls == []


# ---------------- (1) fail-closed verdicts, (4) defect-on/off denominators ----------------
import hashlib, importlib.util
from qa_agent.eval_metrics import verdict, check_thresholds, HARD_GATES, near_copy_similarity, NEAR_COPY_THRESHOLD
from qa_agent.providers.models import compose_plan_story
from qa_agent.util import canon


def harness():
    spec = importlib.util.spec_from_file_location("eval_b2", ROOT / "scripts/eval_b2.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


GOOD = {"ac_precision": 1, "ac_recall": 1, "required_coverage": 1, "required_categories_present": 1, "skill_action_correct_rate": 1,
        "gap_recall": 1, "gap_precision": 1, "injection_flagged_rate": 1, "policy_violations": 0, "schema_valid_first_try_rate": 1,
        "mean_repairs": 0, "seeded_defect_detection": 1, "false_alarms": 0, "execution_errors": 0, "repeatability": 1,
        "plan_latency_p95_s": 1, "contaminated_plans": 0, "defect_off_runs_incomplete": 0}
FULL = {"mode": "full", "complete": True, "missing": []}


@pytest.mark.eval
def test_verdict_pass_only_when_complete_and_measured():
    assert verdict(GOOD, check_thresholds(GOOD), FULL) == "PASS"
    for k in HARD_GATES:                                            # any unmeasured hard gate => never PASS
        g = dict(GOOD); g[k] = None
        assert verdict(g, check_thresholds(g), FULL) == "NOT_EVALUABLE", k
    g = dict(GOOD); g["repeatability"] = None                       # unmeasured soft metric => not PASS either
    assert verdict(g, check_thresholds(g), FULL) == "NOT_EVALUABLE"
    assert verdict(GOOD, check_thresholds(GOOD), {"mode": "full", "complete": False, "missing": ["x"]}) == "NOT_EVALUABLE"
    assert verdict(GOOD, check_thresholds(GOOD), {"mode": "smoke", "complete": True, "missing": []}) == "SMOKE/NOT_EVALUABLE"
    g = dict(GOOD); g["policy_violations"] = 1
    assert verdict(g, check_thresholds(g), FULL) == "FAIL"
    g = dict(GOOD); g["ac_recall"] = 0.5
    assert verdict(g, check_thresholds(g), FULL) == "FAIL"


def fake_run(story, phase, planned=True, executed=True, detected=(), failed_tests=(), status="completed", sig=("a",)):
    q = {"ac_precision": 1.0, "ac_recall": 1.0, "required_coverage": 1.0, "required_categories_present": 1.0, "skill_action_correct_rate": 1.0,
         "gap_recall": 1.0, "gap_precision": 1.0, "injection_flagged": None, "contaminated": False, "near_copy": None, "test_signatures": list(sig)}
    if not planned:
        q = {k: (0.0 if isinstance(v, float) else v) for k, v in q.items()}; q["test_signatures"] = []
    ex = {"status": status, "detected": list(detected), "failed_tests": list(failed_tests), "execution_errors": 0} if (planned and executed) else None
    return {"story": story, "phase": phase, "planned": planned, "quality": q, "execution": ex, "policy_violations": 0, "format_errors": 0,
            "schema_valid_first_try": planned, "latency_s": 0.1, "tokens_total_actual": 0, "cost_usd_actual": 0, "unpriced_calls": 0}


@pytest.mark.eval
def test_failed_and_missing_runs_stay_in_denominators():
    h = harness()
    golds = {"PCC-DEMO-5": load_json(ROOT / "fixtures/eval/gold/PCC-DEMO-5.gold.json")}   # 1 must-fail oracle
    runs = [fake_run("PCC-DEMO-5", "defect_on", detected=["ORC5-ALIAS-5101"]),
            fake_run("PCC-DEMO-5", "defect_on", planned=False),                   # planning failed
            fake_run("PCC-DEMO-5", "defect_on", executed=False),                  # no execution result
            fake_run("PCC-DEMO-5", "defect_off"), fake_run("PCC-DEMO-5", "defect_off", status="failed"),
            fake_run("PCC-DEMO-5", "defect_off", failed_tests=["TC-01"])]
    a = h.aggregate(runs, golds, "stub")
    assert a["seeded_defect_detection"] == round(1 / 3, 4) and a["seeded_defects_expected"] == 3
    assert a["planning_failures"] == 1 and a["ac_recall"] == round(5 / 6, 4)
    assert a["defect_off_runs_incomplete"] == 1 and a["false_alarms"] == 1
    assert verdict(a, check_thresholds(a), FULL) == "FAIL"


@pytest.mark.eval
def test_defect_off_phase_missing_is_not_evaluable():
    h = harness()
    golds = {"PCC-DEMO-5": load_json(ROOT / "fixtures/eval/gold/PCC-DEMO-5.gold.json")}
    a = h.aggregate([fake_run("PCC-DEMO-5", "defect_on", detected=["ORC5-ALIAS-5101"])] * 3, golds, "stub")
    assert a["false_alarms"] is None and a["defect_off_runs_incomplete"] is None
    assert verdict(a, check_thresholds(a), FULL) == "NOT_EVALUABLE"


@pytest.mark.eval
@pytest.mark.integration
def test_full_run_pauses_for_first_plan_review_without_simulation(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "stub-not-a-real-key-000000")
    rc = harness().main(["--provider", "stub", "--out", str(tmp_path), "--runs", "3"])
    rep = json.loads((tmp_path / "eval_report.json").read_text())
    assert rc == 4 and rep["verdict"] == "PAUSED/AWAITING_FIRST_PLAN_APPROVAL" and "first live plan requires manual review" in rep["first_plan"]["reason"]


@pytest.mark.eval
@pytest.mark.integration
def test_plan_only_is_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "stub-not-a-real-key-000000")
    rc = harness().main(["--provider", "stub", "--out", str(tmp_path), "--runs", "3", "--no-execute"])
    rep = json.loads((tmp_path / "eval_report.json").read_text())
    assert rc == 3 and rep["verdict"] == "SMOKE/NOT_EVALUABLE" and rep["phases"] == ["plan_only"]


# ---------------- (3) held-out story, sealed gold, contamination heuristics ----------------
@pytest.mark.eval
def test_held_out_story_has_no_recording_and_no_gold_in_repo():
    es = load_json(ROOT / "fixtures/eval/eval_set.json")
    assert es["held_out_stories"] == ["PCC-DEMO-7"] and "PCC-DEMO-7" not in es["required_stories"]
    assert not (ROOT / "fixtures/replay/PCC-DEMO-7.json").exists()
    assert not list(ROOT.glob("**/PCC-DEMO-7.gold.json"))
    assert len(es["sealed_gold"]["PCC-DEMO-7"]["sha256"]) == 64
    assert (ROOT / "docs/b2/blinded/PCC-DEMO-7.SPEC.md").exists()
    o = load_json(ROOT / "fixtures/oracles/PCC-DEMO-7.expected.json")
    assert o["review"]["status"] == "synthetic-pending-qa-lead-approval" and (ROOT / "fixtures/data/supplier_lengths.csv").exists()


@pytest.mark.eval
def test_sealed_gold_hash_verification(tmp_path):
    h = harness()
    gold = {"story": "PCC-DEMO-7", "x": 1}
    es = {"sealed_gold": {"PCC-DEMO-7": {"sha256": hashlib.sha256(canon(gold).encode()).hexdigest()}}}
    assert h.load_sealed_gold(es, "PCC-DEMO-7", None)[1].startswith("sealed gold not provided")
    assert "missing" in h.load_sealed_gold(es, "PCC-DEMO-7", tmp_path)[1]
    (tmp_path / "PCC-DEMO-7.gold.json").write_text(json.dumps(gold))
    assert h.load_sealed_gold(es, "PCC-DEMO-7", tmp_path) == (gold, None)
    (tmp_path / "PCC-DEMO-7.gold.json").write_text(json.dumps({**gold, "x": 2}))
    assert h.load_sealed_gold(es, "PCC-DEMO-7", tmp_path)[1] == "sealed gold hash mismatch"


@pytest.mark.eval
def test_near_copy_heuristic():
    rec = compose_plan_story(load_json(ROOT / "fixtures/replay/PCC-DEMO-6.json"))
    assert near_copy_similarity(rec, rec) == 1.0
    para = copy.deepcopy(rec)
    for t in para["test_cases"]:
        t["title"] = t["title"].replace("API:", "Api -")              # light paraphrase defeats exact equality
    assert canon(para) != canon(rec) and near_copy_similarity(para, rec) >= NEAR_COPY_THRESHOLD
    other = compose_plan_story(load_json(ROOT / "fixtures/replay/PCC-DEMO-3.json"))
    assert near_copy_similarity(other, rec) < NEAR_COPY_THRESHOLD
