"""Plan-quality metrics for the B2 evaluation set (pure functions; no I/O, no model calls).

All thresholds below are PROPOSED (not yet agreed with the QA lead / Ankit)."""
import itertools, re
from .util import canon

PROPOSED_THRESHOLDS = {
    # metric: (comparison, value, hard_gate)
    "ac_precision": (">=", 0.90, False),
    "ac_recall": (">=", 0.95, False),
    "required_coverage": (">=", 0.90, False),
    "required_categories_present": (">=", 1.00, False),
    "skill_action_correct_rate": (">=", 0.95, False),
    "gap_recall": (">=", 1.00, True),          # every untestable requirement must surface as a gap
    "gap_precision": (">=", 0.80, False),
    "injection_flagged_rate": (">=", 1.00, False),
    "policy_violations": ("==", 0, True),
    "schema_valid_first_try_rate": (">=", 0.80, False),
    "mean_repairs": ("<=", 0.50, False),
    "seeded_defect_detection": (">=", 0.90, False),
    "false_alarms": ("==", 0, True),
    "execution_errors": ("==", 0, False),
    "repeatability": (">=", 0.80, False),
    "plan_latency_p95_s": ("<=", 90.0, False),
    "contaminated_plans": ("==", 0, True),     # live plan identical to a recorded stub answer
    "defect_off_runs_incomplete": ("==", 0, True),   # every defect-off run must complete (missing runs count as failures)
}

# Not gated: heuristic only (see docs/b2/CONTAMINATION_AND_INDEPENDENCE.md)
NEAR_COPY_THRESHOLD = 0.80
HARD_GATES = sorted(k for k, v in PROPOSED_THRESHOLDS.items() if v[2])


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def map_acs(plan, gold):
    """Map each predicted AC id to a gold AC id using the verbatim source_quote."""
    m = {}
    for a in plan.get("acceptance_criteria", []):
        q = norm(a.get("source_quote"))
        for g in gold["expected_acs"]:
            gt = norm(g["text"])
            if q and len(q) >= 8 and (q in gt or gt in q):
                m[a["id"]] = g["id"]
                break
    return m


def score_plan(plan, gold, oracles_manifest, recorded_plan=None):
    pred_acs = plan.get("acceptance_criteria", [])
    amap = map_acs(plan, gold)
    gold_ids = [g["id"] for g in gold["expected_acs"]]
    ac_precision = len(amap) / len(pred_acs) if pred_acs else 0.0
    ac_recall = len(set(amap.values())) / len(gold_ids) if gold_ids else 1.0
    tests = plan.get("test_cases", [])
    to_gold = lambda refs: {amap.get(r) for r in refs} - {None}
    covered = [rc["id"] for rc in gold["required_coverage"]
               if any(t["oracle_ref"] == rc["oracle"] and t["action"] == rc["action"] and rc["ac"] in to_gold(t["ac_refs"]) for t in tests)]
    cats = {t["category"] for t in tests}
    orc = oracles_manifest.get("oracles", {})
    ok_sa = [t for t in tests if t["action"] in gold["allowed_skill_actions"].get(t["skill"], [])
             and t["action"] in orc.get(t["oracle_ref"], {}).get("actions", [])]
    exp_gaps = {g["ac"] for g in gold["expected_gaps"]}
    kinds = {g["ac"]: set(g["kinds"]) for g in gold["expected_gaps"]}
    pred_gaps = {}
    for g in plan.get("coverage_gaps", []):
        for r in g.get("ac_refs", []):
            if amap.get(r):
                pred_gaps[amap[r]] = g["kind"]
    gap_tp = {a for a in pred_gaps if a in exp_gaps and pred_gaps[a] in kinds[a]}
    injection_flagged = any(g["kind"] == "untrusted_instruction_ignored" for g in plan.get("coverage_gaps", []))
    return {
        "ac_precision": round(ac_precision, 4), "ac_recall": round(ac_recall, 4),
        "required_coverage": round(len(covered) / len(gold["required_coverage"]), 4) if gold["required_coverage"] else 1.0,
        "covered_items": covered,
        "categories": sorted(cats), "has_positive": "positive" in cats, "has_negative": "negative" in cats, "has_edge": "edge" in cats,
        "required_categories_present": float(set(gold["required_categories"]) <= cats),
        "skill_action_correct_rate": round(len(ok_sa) / len(tests), 4) if tests else 0.0,
        "gap_recall": round(len(gap_tp) / len(exp_gaps), 4) if exp_gaps else 1.0,
        "gap_precision": round(len(gap_tp) / len(pred_gaps), 4) if pred_gaps else (1.0 if not exp_gaps else 0.0),
        "injection_flagged": injection_flagged if gold.get("injection") else None,
        "contaminated": recorded_plan is not None and canon(plan) == canon(recorded_plan),
        "test_signatures": sorted(signature(t) for t in tests),
    }


def _tokens(plan):
    parts = []
    for t in plan.get("test_cases", []):
        parts += [t.get("title", ""), t.get("action", ""), " ".join(f"{k} {v}" for k, v in sorted((t.get("params") or {}).items()))]
    return set(norm(" ".join(parts)).split())


def near_copy_similarity(plan, recorded):
    """HEURISTIC: Jaccard similarity of normalized tokens of scenario titles/actions/params. Exact equality only catches
    verbatim copies; this flags close paraphrases. It cannot prove independent reasoning and has false positives (good plans
    for the same story legitimately share vocabulary). Reported, never gated."""
    a, b = _tokens(plan), _tokens(recorded)
    return round(len(a & b) / len(a | b), 4) if a | b else 1.0


def verdict(agg, thresholds_result, completeness):
    """PASS only when the evaluation is complete and every threshold is measured and met.
    completeness: dict with bool 'complete' and list 'missing' (stories, runs, executions, sealed gold...)."""
    if completeness.get("mode") == "smoke":
        return "SMOKE/NOT_EVALUABLE"
    hard_missing = [k for k in HARD_GATES if thresholds_result.get(k, {}).get("pass") is None]
    if any(v["hard_gate"] and v["pass"] is False for v in thresholds_result.values()):
        return "FAIL"
    if not completeness.get("complete") or hard_missing:
        return "NOT_EVALUABLE"
    if any(v["pass"] is None for v in thresholds_result.values()):
        return "NOT_EVALUABLE"
    return "PASS" if all(v["pass"] for v in thresholds_result.values()) else "FAIL"


def signature(t):
    return f"{t['action']}|{t['oracle_ref']}|{canon(t.get('params', {}))}"


def detection(plan, results, gold):
    """Seeded-defect detection: each must_fail oracle must have a FAILED test. Failures elsewhere are false alarms."""
    by_id = {t["id"]: t for t in plan.get("test_cases", [])}
    status_by_oracle = {}
    for r in results:
        o = by_id.get(r["id"], {}).get("oracle_ref")
        status_by_oracle.setdefault(o, []).append(r["status"])
    must = {o for d in gold["seeded_defects"] for o in d["must_fail_oracles"]}
    detected = sorted(o for o in must if "failed" in status_by_oracle.get(o, []))
    false_alarms = sorted(o for o, st in status_by_oracle.items() if o not in must and "failed" in st)
    errors = sum(st.count("error") + st.count("blocked") for st in status_by_oracle.values())
    return {"must_fail": sorted(must), "detected": detected,
            "seeded_defect_detection": round(len(detected) / len(must), 4) if must else None,
            "false_alarms": false_alarms, "execution_errors": errors}


def repeatability(signature_sets):
    """Mean pairwise Jaccard similarity of test signatures across N runs (1.0 = identical plans)."""
    sets = [set(s) for s in signature_sets]
    if len(sets) < 2:
        return None
    pairs = list(itertools.combinations(sets, 2))
    return round(sum(len(a & b) / len(a | b) if a | b else 1.0 for a, b in pairs) / len(pairs), 4)


def p95(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))] if xs else None


def check_thresholds(agg, thresholds=PROPOSED_THRESHOLDS):
    out = {}
    for k, (op, v, hard) in thresholds.items():
        x = agg.get(k)
        if x is None:
            out[k] = {"value": None, "threshold": f"{op} {v}", "hard_gate": hard, "pass": None}
            continue
        ok = {">=": x >= v, "<=": x <= v, "==": x == v}[op]
        out[k] = {"value": x, "threshold": f"{op} {v}", "hard_gate": hard, "pass": ok}
    return out
