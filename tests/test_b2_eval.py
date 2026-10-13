"""B2 evaluation set, gold annotations, metrics and harness. No live model calls."""
import copy, importlib.util, json, os, re, socket
import pytest
from qa_agent import ROOT
from qa_agent.eval_metrics import score_plan, detection, repeatability, check_thresholds, map_acs, PROPOSED_THRESHOLDS
from qa_agent.planning import validate_plan, build_messages, build_catalog
from qa_agent.providers.models import compose_plan_story
from qa_agent.skills import LocalSkillRegistry
from qa_agent.util import load_json

pytestmark = [pytest.mark.eval]
ES = load_json(ROOT / "fixtures/eval/eval_set.json")
STORIES = ES["stories"]
SK = LocalSkillRegistry()
FILES = sorted(p.name for p in (ROOT / "fixtures/data").glob("*.csv"))
story = lambda k: load_json(ROOT / f"fixtures/jira/{k}.json")
gold = lambda k: load_json(ROOT / f"fixtures/eval/gold/{k}.gold.json")
oracles = lambda k: load_json(ROOT / f"fixtures/oracles/{k}.expected.json")
recorded = lambda k: compose_plan_story(load_json(ROOT / f"fixtures/replay/{k}.json"))


def load_harness():
    spec = importlib.util.spec_from_file_location("eval_b2", ROOT / "scripts/eval_b2.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


# ---------------- dataset ----------------
def test_eval_set_composition():
    assert STORIES == ["PCC-DEMO-2", "PCC-DEMO-3", "PCC-DEMO-4", "PCC-DEMO-5", "PCC-DEMO-6"] and "PCC-DEMO-1" not in STORIES
    assert set(ES["prompt_injection_stories"]) <= set(STORIES) and ES["prompt_injection_stories"]
    assert set(ES["untestable_requirement_stories"]) <= set(STORIES) and ES["untestable_requirement_stories"]
    for k in ("PCC-DEMO-5", "PCC-DEMO-6"):
        s = story(k)
        assert "SYNTHETIC" in s["_provenance"] and "b2-eval" in s["fields"]["labels"]
    assert "prompt-injection-test" in story("PCC-DEMO-6")["fields"]["labels"]
    assert "untestable-requirement" in story("PCC-DEMO-5")["fields"]["labels"]


@pytest.mark.parametrize("k", STORIES)
def test_gold_is_consistent_with_story_and_oracles(k):
    g, o, desc = gold(k), oracles(k), story(k)["fields"]["description"]
    assert g["review"]["status"] == "synthetic-pending-qa-lead-approval"
    assert o["review"]["status"] == "synthetic-pending-qa-lead-approval" and "NOT derived from application code" in o["_provenance"]
    for ac in g["expected_acs"]:
        assert ac["text"] in desc                                         # verbatim from story
    ids = {a["id"] for a in g["expected_acs"]}
    for rc in g["required_coverage"]:
        assert rc["ac"] in ids and rc["action"] in o["oracles"][rc["oracle"]]["actions"] and rc["ac"] in o["oracles"][rc["oracle"]]["ac"]
    oracle_acs = {a for x in o["oracles"].values() for a in x["ac"]}
    for gap in g["expected_gaps"]:
        assert gap["ac"] in ids and gap["ac"] not in oracle_acs             # an expected gap has no approved oracle
    assert set(g["untestable"]) == {x["ac"] for x in g["expected_gaps"]}
    sut_src = (ROOT / "sut/app.py").read_text()
    for d in g["seeded_defects"]:
        assert f'"{d["defect"]}"' in sut_src and set(d["must_fail_oracles"]) <= set(o["oracles"])
    for x in o["oracles"].values():
        for f in re.findall(r"supplier_[a-z_]+\.csv", json.dumps(x)):
            assert (ROOT / "fixtures/data" / f).exists()


@pytest.mark.parametrize("k", STORIES)
def test_stub_answers_validate_and_are_harness_only(k):
    p = recorded(k)
    validate_plan(copy.deepcopy(p), story(k)["fields"]["description"], SK, oracles(k), FILES)
    if k in ("PCC-DEMO-5", "PCC-DEMO-6"):
        assert "ONLY to self-test" in load_json(ROOT / f"fixtures/replay/{k}.json")["_provenance"]


@pytest.mark.parametrize("k", STORIES)
def test_prompt_never_contains_recorded_answers(k):
    cat = build_catalog(SK, oracles(k), FILES)
    msgs = json.dumps(build_messages("plan_story", {"story_key": k, "story": story(k), "catalog": cat, "data_class": "synthetic"}))
    p = recorded(k)
    for s in [t["title"] for t in p["test_cases"]] + [g["reason"] for g in p["coverage_gaps"]] + [p["understanding"]["summary"]]:
        assert json.dumps(s)[1:-1] not in msgs, s
    assert "STUB/REPLAY" not in msgs


# ---------------- SUT seeded defects ----------------
def test_new_sut_behaviour_and_defects(monkeypatch):
    from sut import app as sut
    nm = (ROOT / "fixtures/data/supplier_missing_name.csv").read_text()
    alias = (ROOT / "fixtures/data/supplier_etim_alias.csv").read_text()
    monkeypatch.setenv("SUT_DEFECTS", "")
    sut.PRODUCTS.clear(); r = sut.process_csv(nm)
    assert (r["imported"], r["rejected"], {e["code"] for e in r["errors"]}) == (2, 1, {"NAME_REQUIRED"})
    sut.PRODUCTS.clear(); sut.process_csv(alias)
    assert sut.PRODUCTS["SKU-5101"]["attributes"]["etim_class"] == "EC-SYN-0003"
    monkeypatch.setenv("SUT_DEFECTS", "name_not_required,etim_alias_ignored")
    sut.PRODUCTS.clear(); assert sut.process_csv(nm)["imported"] == 3
    sut.PRODUCTS.clear(); sut.process_csv(alias)
    assert "etim_class" not in sut.PRODUCTS["SKU-5101"]["attributes"]


# ---------------- metrics ----------------
def test_metrics_perfect_plan():
    for k in STORIES:
        s = score_plan(recorded(k), gold(k), oracles(k))
        assert (s["ac_precision"], s["ac_recall"], s["required_coverage"], s["gap_recall"], s["skill_action_correct_rate"]) == (1, 1, 1, 1, 1), k
        assert s["injection_flagged"] in ((True,) if gold(k)["injection"] else (None,))


def test_metrics_degraded_plan():
    k = "PCC-DEMO-6"; p = recorded(k)
    p["acceptance_criteria"].append({"id": "AC9", "text": "made up", "source_quote": "the system must support SAP export"})
    p["test_cases"] = [t for t in p["test_cases"] if t["oracle_ref"] != "ORC6-UI-ERR"]
    p["test_cases"][0]["action"] = "ui_import_and_count"                      # wrong action for oracle
    p["coverage_gaps"] = []                                                    # misses untestable AC5 + injection
    s = score_plan(p, gold(k), oracles(k))
    assert s["ac_precision"] == round(5 / 6, 4) and s["ac_recall"] == 1.0
    assert s["required_coverage"] == 0.5 and s["skill_action_correct_rate"] == round(2 / 3, 4)
    assert s["gap_recall"] == 0.0 and s["injection_flagged"] is False
    assert not check_thresholds({"gap_recall": s["gap_recall"]})["gap_recall"]["pass"]


def test_ac_mapping_requires_verbatim_quote():
    g = gold("PCC-DEMO-5")
    p = {"acceptance_criteria": [{"id": "AC1", "text": "x", "source_quote": "etim class mapping works"}]}
    assert map_acs(p, g) == {}


def test_detection_and_false_alarms():
    k = "PCC-DEMO-6"; p = recorded(k); g = gold(k)
    st = {"TC-01": "failed", "TC-02": "passed", "TC-03": "failed", "TC-04": "failed"}
    d = detection(p, [{"id": i, "status": s} for i, s in st.items()], g)
    assert d["seeded_defect_detection"] == 1.0 and d["false_alarms"] == []
    st["TC-02"] = "failed"; st["TC-04"] = "passed"
    d = detection(p, [{"id": i, "status": s} for i, s in st.items()], g)
    assert d["seeded_defect_detection"] == round(2 / 3, 4) and d["false_alarms"] == ["ORC6-VALID-ROW"]


def test_repeatability_and_threshold_gates():
    assert repeatability([["a", "b"], ["a", "b"]]) == 1.0 and repeatability([["a", "b"], ["a", "c"]]) == round(1 / 3, 4)
    assert repeatability([["a"]]) is None
    th = check_thresholds({"policy_violations": 1, "false_alarms": 0})
    assert th["policy_violations"]["pass"] is False and th["policy_violations"]["hard_gate"] is True
    assert PROPOSED_THRESHOLDS["policy_violations"] == ("==", 0, True)


# ---------------- harness ----------------
def test_harness_grok_requires_explicit_confirmation(tmp_path):
    with pytest.raises(SystemExit, match="confirm-live-approved"):
        load_harness().main(["--provider", "grok", "--out", str(tmp_path)])


def test_harness_grok_refuses_stub_endpoint(monkeypatch):
    h = load_harness()
    cfg = load_json(ROOT / "config/agent.json"); cfg["providers"]["grok"]["base_url"] = "http://127.0.0.1:8701/v1"
    monkeypatch.setattr(h, "load_json", lambda p: cfg)
    with pytest.raises(SystemExit, match="loopback"):
        h.agent_config("grok")


@pytest.mark.integration
def test_harness_grok_fails_closed_with_default_config(tmp_path, monkeypatch):
    real = socket.socket.connect
    def guard(self, addr):
        if isinstance(addr, tuple) and addr[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"external network attempted: {addr}")
        return real(self, addr)
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    rc = load_harness().main(["--provider", "grok", "--confirm-live-approved", "--out", str(tmp_path), "--runs", "1",
                              "--stories", "PCC-DEMO-5"])
    rep = json.loads((tmp_path / "eval_report.json").read_text())
    assert rc == 2 and rep["verdict"] == "NOT_EVALUABLE" and "not enabled" in rep["error"]


@pytest.mark.integration
def test_harness_stub_subset_is_smoke_never_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "stub-not-a-real-key-000000")
    rc = load_harness().main(["--provider", "stub", "--out", str(tmp_path), "--runs", "1", "--stories", "PCC-DEMO-5,PCC-DEMO-6",
                              "--self-test-simulated-review"])
    rep = json.loads((tmp_path / "eval_report.json").read_text())
    assert rc == 3 and rep["verdict"] == "SMOKE/NOT_EVALUABLE" and rep["mode"] == "smoke" and "NOT real model reasoning" in rep["banner"]
    assert any("required story PCC-DEMO-2 not run" in m for m in rep["completeness"]["missing"])
    a = rep["aggregate"]
    assert a["policy_violations"] == 0 and a["seeded_defect_detection"] == 1.0 and a["false_alarms"] == 0 and a["defect_off_runs_incomplete"] == 0
    assert all("STUB ENDPOINT" in l for l in rep["model_labels"])
    assert (tmp_path / "eval_report.md").read_text().startswith("# B2 plan-quality evaluation")
