#!/usr/bin/env python3
"""B2 plan-quality evaluation harness (fail-closed verdicts: PASS / FAIL / NOT_EVALUABLE / SMOKE/NOT_EVALUABLE).

  python scripts/eval_b2.py --provider stub --authorization fixtures/eval/authorizations/stub-selftest.json   # full self-test
  python scripts/eval_b2.py --provider stub --no-execute --stories PCC-DEMO-5 --runs 1                       # smoke
  python scripts/eval_b2.py --provider grok --confirm-live-approved --authorization <signed file> \\
         --sealed-gold-dir <custody dir>                                                                      # ONLY after sign-off

* replay/stub = HARNESS SELF-TEST: hand-authored recordings, NOT model reasoning. Held-out stories are skipped there.
* A full evaluation needs: every required story, >= min_runs_per_story runs, a defect-ON and a defect-OFF phase with
  execution results for every run, and (live) the sealed gold for held-out stories. Anything less is never PASS.
* Failed/missing runs stay in every denominator (scored as zero / not detected / incomplete).
* Execution needs approval. The harness never bypasses it: without --authorization runs stop at awaiting_approval
  (=> NOT_EVALUABLE). With it, approval goes through QAAgent.approve_unattended (scoped, audited, runtime-enforced).
* Cost/tokens are reported from actual usage only.
"""
import argparse, contextlib, copy, hashlib, json, os, socket, subprocess, sys, time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx  # noqa: E402
from qa_agent.eval_metrics import (score_plan, detection, repeatability, p95, check_thresholds, verdict, near_copy_similarity,  # noqa: E402
                                   NEAR_COPY_THRESHOLD, PROPOSED_THRESHOLDS)
from qa_agent.util import load_json, canon  # noqa: E402

STUB_PRICING = {"input_per_mtok_usd": 1.0, "cached_input_per_mtok_usd": 0.25, "output_per_mtok_usd": 5.0}  # FAKE, stub only
ZERO_Q = {"ac_precision": 0.0, "ac_recall": 0.0, "required_coverage": 0.0, "required_categories_present": 0.0,
          "skill_action_correct_rate": 0.0, "gap_recall": 0.0, "gap_precision": 0.0, "test_signatures": []}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


@contextlib.contextmanager
def serve(module, env_extra=None, probe="/"):
    port = free_port()
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT,
                         env=dict(os.environ, **(env_extra or {})), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                if httpx.get(url + probe).status_code < 500:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise RuntimeError(f"{module} did not start")
        yield url
    finally:
        p.terminate(); p.wait(10)


def agent_config(provider, stub_url=None):
    cfg = copy.deepcopy(load_json(ROOT / "config/agent.json"))
    if provider == "replay":
        cfg["model"]["provider"] = "replay"
    elif provider == "stub":
        cfg["model"]["provider"] = "grok"
        cfg["providers"]["grok"].update(enabled=True, base_url=stub_url + "/v1", pricing=dict(STUB_PRICING), timeout_s=30, max_retries=1)
        cfg["budget"].update(scope="stub-selftest", total_usd=1000.0, daily_usd=1000.0)   # FAKE prices; isolated scope
    elif provider == "grok":
        cfg["model"]["provider"] = "grok"
        host = urlparse(cfg["providers"]["grok"].get("base_url", "")).hostname or ""
        if host in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit("refusing: --provider grok points at a loopback/stub endpoint; live eval must not use recorded answers")
    if provider in ("stub", "replay"):
        cfg.setdefault("unattended_eval_approval", {})["enabled"] = True   # self-test only; still needs the authorization file
    return cfg


def load_sealed_gold(es, key, sealed_dir):
    meta = es["sealed_gold"][key]
    if not sealed_dir:
        return None, "sealed gold not provided (--sealed-gold-dir)"
    p = Path(sealed_dir) / f"{key}.gold.json"
    if not p.exists():
        return None, f"sealed gold file missing: {p.name}"
    g = json.loads(p.read_text())
    if hashlib.sha256(canon(g).encode()).hexdigest() != meta["sha256"]:
        return None, "sealed gold hash mismatch"
    return g, None


def run_story(agent, key, gold, phase, execute, authorization, recorded, provider):
    t0 = time.monotonic()
    rid = agent.start_run(f"Plan {key}", story_key=key, wait=True)
    latency = round(time.monotonic() - t0, 3)
    run = agent.store.get_run(rid)
    kinds = [t["kind"] for t in agent.store.q("SELECT kind FROM telemetry WHERE run_id=?", (rid,))]
    usage = agent.store.run_usage(rid)
    priced = [c for c in usage["calls"] if c.get("priced")]
    rec = {"run_id": rid, "story": key, "phase": phase, "status": run["status"], "error": run.get("error"),
           "model_label": run.get("model_label"), "latency_s": latency,
           "policy_violations": kinds.count("policy_violation"), "format_errors": kinds.count("format_error"),
           "schema_valid_first_try": run["status"] == "awaiting_approval" and kinds.count("format_error") == 0,
           "tokens_total_actual": sum(c["total_tokens"] or 0 for c in priced),
           "cost_usd_actual": round(sum(c["cost_usd"] or 0 for c in priced), 6), "unpriced_calls": len(usage["calls"]) - len(priced),
           "planned": run["status"] == "awaiting_approval", "execution": None, "execution_missing_reason": None}
    if not rec["planned"]:
        rec["quality"] = dict(ZERO_Q, injection_flagged=False if gold.get("injection") else None, contaminated=False, near_copy=None)
        rec["execution_missing_reason"] = "planning failed"
        return rec
    plan = run["planning_json"]
    oracles = load_json(ROOT / f"fixtures/oracles/{key}.expected.json")
    q = score_plan(plan, gold, oracles, recorded if provider == "grok" else None)
    q["near_copy"] = near_copy_similarity(plan, recorded) if (recorded is not None and provider == "grok") else None
    rec["quality"] = q
    if not execute:
        rec["execution_missing_reason"] = "plan-only (smoke)"
        return rec
    if not authorization:
        rec["execution_missing_reason"] = "awaiting human approval (no --authorization)"
        return rec
    from qa_agent.eval_authorization import AuthorizationError
    try:
        agent.approve_unattended(rid, authorization, wait=True)
    except (AuthorizationError, ValueError) as e:
        rec["execution_missing_reason"] = f"approval refused: {e}"
        return rec
    fin = agent.store.get_run(rid)
    rec["execution"] = {"status": fin["status"], **detection(plan, agent.store.results(rid), gold),
                        "failed_tests": sorted(r["id"] for r in agent.store.results(rid) if r["status"] == "failed")}
    return rec


def aggregate(runs, golds, provider):
    mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else None
    q = [r["quality"] for r in runs]
    on = [r for r in runs if r["phase"] == "defect_on"]
    off = [r for r in runs if r["phase"] == "defect_off"]
    # seeded-defect recall over ALL defect-on runs: missing/failed runs count as 'not detected'
    exp_total = sum(len({o for d in golds[r["story"]]["seeded_defects"] for o in d["must_fail_oracles"]}) for r in on)
    det_total = sum(len(r["execution"]["detected"]) for r in on if r["execution"])
    off_exec = [r for r in off if r["execution"] and r["execution"]["status"] == "completed"]
    inj = [r["quality"]["injection_flagged"] for r in runs if r["quality"].get("injection_flagged") is not None]
    executed = [r for r in runs if r["execution"]]
    by_story = {}
    for r in runs:
        by_story.setdefault((r["story"], r["phase"]), []).append(r["quality"]["test_signatures"])
    reps = [repeatability(v) for v in by_story.values() if len(v) > 1]
    nc = [r["quality"]["near_copy"] for r in runs if r["quality"].get("near_copy") is not None]
    return {
        "runs": len(runs), "planned_ok": sum(r["planned"] for r in runs), "planning_failures": sum(not r["planned"] for r in runs),
        "ac_precision": mean([x["ac_precision"] for x in q]), "ac_recall": mean([x["ac_recall"] for x in q]),
        "required_coverage": mean([x["required_coverage"] for x in q]),
        "required_categories_present": mean([x["required_categories_present"] for x in q]),
        "skill_action_correct_rate": mean([x["skill_action_correct_rate"] for x in q]),
        "gap_recall": mean([x["gap_recall"] for x in q]), "gap_precision": mean([x["gap_precision"] for x in q]),
        "injection_flagged_rate": mean([float(x) for x in inj]),
        "policy_violations": sum(r["policy_violations"] for r in runs),
        "schema_valid_first_try_rate": mean([float(r["schema_valid_first_try"]) for r in runs]),
        "mean_repairs": mean([r["format_errors"] for r in runs]),
        "seeded_defect_detection": round(det_total / exp_total, 4) if on and exp_total else None,
        "seeded_defects_expected": exp_total, "seeded_defects_detected": det_total,
        # defect-OFF: failed planning or non-completed execution counts as a failure; runs never executed for lack of an
        # approval are not failures but make the evaluation incomplete (reported via completeness => NOT_EVALUABLE)
        "false_alarms": sum(len(r["execution"]["failed_tests"]) for r in off_exec) if off_exec else None,
        "defect_off_runs_incomplete": (sum(1 for r in off if not r["planned"]) + sum(1 for r in off if r["execution"] and r["execution"]["status"] != "completed"))
                                      if off else None,
        "execution_errors": (sum(r["execution"]["execution_errors"] for r in executed)
                             + sum(1 for r in executed if r["execution"]["status"] != "completed")) if executed else None,
        "repeatability": mean(reps),
        "plan_latency_p95_s": p95([r["latency_s"] for r in runs]),
        "contaminated_plans": sum(1 for x in q if x.get("contaminated")) if provider == "grok" else 0,
        "near_copy_max": max(nc) if nc else None, "near_copy_over_threshold": sum(1 for x in nc if x >= NEAR_COPY_THRESHOLD) if nc else None,
        "tokens_total_actual": sum(r["tokens_total_actual"] for r in runs),
        "cost_usd_actual": round(sum(r["cost_usd_actual"] for r in runs), 6), "unpriced_calls": sum(r["unpriced_calls"] for r in runs),
    }


def markdown(rep):
    a, th = rep["aggregate"], rep["thresholds"]
    L = [f"# B2 plan-quality evaluation — provider `{rep['provider']}` — verdict **{rep['verdict']}**", "", f"> {rep['banner']}", "",
         f"Mode: `{rep['mode']}` · stories: {', '.join(rep['stories'])} · runs/story/phase: {rep['runs_per_story']} · phases: {', '.join(rep['phases'])}", ""]
    if rep["completeness"]["missing"]:
        L += ["**Incomplete / not evaluable because:**", ""] + [f"- {m}" for m in rep["completeness"]["missing"]] + [""]
    if rep.get("held_out"):
        L += [f"Held-out: {json.dumps(rep['held_out'])}", ""]
    L += ["| metric | value | PROPOSED threshold | hard gate | pass |", "|---|---|---|---|---|"]
    for k, v in th.items():
        L.append(f"| {k} | {v['value']} | {v['threshold']} | {'yes' if v['hard_gate'] else ''} | "
                 f"{'n/a (missing)' if v['pass'] is None else ('✅' if v['pass'] else '❌')} |")
    L += ["", f"Seeded-defect recall (defect-ON): {a['seeded_defects_detected']}/{a['seeded_defects_expected']} · "
              f"false alarms (defect-OFF): {a['false_alarms']} · incomplete defect-OFF runs: {a['defect_off_runs_incomplete']} · "
              f"planning failures: {a['planning_failures']}/{a['runs']}",
          f"Near-copy similarity (heuristic, not gated): max {a['near_copy_max']}, ≥{NEAR_COPY_THRESHOLD}: {a['near_copy_over_threshold']}",
          f"Tokens (actual): {a['tokens_total_actual']} · cost (actual usage × configured price): ${a['cost_usd_actual']} · unpriced calls: {a['unpriced_calls']}", ""]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["replay", "stub", "grok"], required=True)
    ap.add_argument("--runs", type=int)
    ap.add_argument("--stories")
    ap.add_argument("--out", default="reports/eval")
    ap.add_argument("--no-execute", action="store_true")
    ap.add_argument("--authorization")
    ap.add_argument("--sealed-gold-dir")
    ap.add_argument("--confirm-live-approved", action="store_true")
    a = ap.parse_args(argv)
    if a.provider == "grok" and not a.confirm_live_approved:
        raise SystemExit("refusing: live eval needs --confirm-live-approved (see docs/b2/LIVE_EVAL_APPROVAL_REQUEST.md)")
    from qa_agent.runtime import QAAgent
    saved = {k: os.environ.get(k) for k in ("SYNTHETIC_SUT_URL", "XAI_API_KEY")}
    try:
        return _run(a, QAAgent)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _run(a, QAAgent):
    es = load_json(ROOT / "fixtures/eval/eval_set.json")
    required, held = es["required_stories"], es["held_out_stories"]
    runs_n = a.runs or es["min_runs_per_story"]
    stories = a.stories.split(",") if a.stories else required + held
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    golds, held_out_info, missing = {}, {}, []
    for k in stories:
        if k in held:
            if a.provider != "grok":
                held_out_info[k] = "NOT_EVALUATED: held-out story has no stub recording by design (live provider only)"
                continue
            g, why = load_sealed_gold(es, k, a.sealed_gold_dir)
            if g is None:
                held_out_info[k] = f"NOT_EVALUABLE: {why}"; missing.append(f"{k}: {why}"); continue
            held_out_info[k] = "scored with verified sealed gold"
            golds[k] = g
        else:
            golds[k] = load_json(ROOT / f"fixtures/eval/gold/{k}.gold.json")
    smoke = a.no_execute or bool(set(required) - set(stories)) or runs_n < es["min_runs_per_story"]
    phases = ["plan_only"] if a.no_execute else list(es["phases"])
    with contextlib.ExitStack() as st:
        stub = None
        if a.provider == "stub":
            stub = st.enter_context(serve("qa_agent.devtools.model_stub:app", probe="/_stub/requests"))
            os.environ.setdefault("XAI_API_KEY", "stub-not-a-real-key-000000")
        cfg = agent_config(a.provider, stub)
        try:
            agent = QAAgent(out / "data", agent_cfg=cfg)
        except Exception as e:   # fail closed (grok disabled / unapproved / unpriced / caps missing)
            rep = {"provider": a.provider, "error": f"{type(e).__name__}: {e}", "verdict": "NOT_EVALUABLE",
                   "mode": "not-run", "completeness": {"complete": False, "missing": ["provider construction failed (fail-closed)"]}}
            (out / "eval_report.json").write_text(json.dumps(rep, indent=2)); print(json.dumps(rep, indent=2)); return 2
        runs = []
        for phase in phases:
            defects = "" if phase == "plan_only" else es["phases"][phase]
            with serve("sut.app:app", {"SUT_DEFECTS": defects}, "/api/health") as sut:
                os.environ["SYNTHETIC_SUT_URL"] = sut
                for k in golds:
                    rec = load_json(ROOT / f"fixtures/replay/{k}.json")["responses"]["plan_story"] if (ROOT / f"fixtures/replay/{k}.json").exists() else None
                    for _ in range(runs_n):
                        runs.append(run_story(agent, k, golds[k], phase, not a.no_execute, a.authorization, rec, a.provider))
    for k in set(required) - set(stories):
        missing.append(f"required story {k} not run")
    if runs_n < es["min_runs_per_story"]:
        missing.append(f"runs per story {runs_n} < required {es['min_runs_per_story']}")
    if a.no_execute:
        missing.append("plan-only: no defect-ON / defect-OFF execution")
    no_exec = [r for r in runs if r["execution"] is None and r["planned"]]
    if no_exec and not a.no_execute:
        reasons = sorted({r["execution_missing_reason"] for r in no_exec})
        missing.append(f"{len(no_exec)} planned run(s) without execution results: {reasons}")
    agg = aggregate(runs, golds, a.provider)
    th = check_thresholds(agg)
    completeness = {"mode": "smoke" if smoke else "full", "complete": not missing, "missing": missing}
    v = verdict(agg, th, completeness)
    banner = ("HARNESS SELF-TEST — answers are hand-authored STUB/REPLAY recordings, NOT real model reasoning. Cost uses FAKE stub pricing."
              if a.provider in ("stub", "replay") else "LIVE MODEL evaluation on synthetic data. Cost from actual usage × configured price.")
    rep = {"provider": a.provider, "banner": banner, "mode": completeness["mode"], "verdict": v, "stories": list(golds),
           "runs_per_story": runs_n, "phases": phases, "completeness": completeness, "held_out": held_out_info,
           "proposed_thresholds": {k: list(x) for k, x in PROPOSED_THRESHOLDS.items()}, "aggregate": agg, "thresholds": th,
           "runs": runs, "model_labels": sorted({r.get("model_label") or "" for r in runs})}
    (out / "eval_report.json").write_text(json.dumps(rep, indent=2))
    (out / "eval_report.md").write_text(markdown(rep))
    print(markdown(rep))
    return {"PASS": 0, "FAIL": 1}.get(v, 3)


if __name__ == "__main__":
    sys.exit(main())
