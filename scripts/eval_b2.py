#!/usr/bin/env python3
"""B2 plan-quality evaluation harness (fail-closed verdicts: PASS / FAIL / NOT_EVALUABLE / SMOKE/NOT_EVALUABLE).

  python scripts/eval_b2.py --provider stub --self-test-simulated-review          # full self-test (stub/replay only)
  python scripts/eval_b2.py --provider stub --no-execute --stories PCC-DEMO-5 --runs 1   # smoke
  Live (ONLY after sign-off), resumable:
    1) python scripts/eval_b2.py --provider <approved-provider> --confirm-live-approved --out reports/eval
         -> plans the FIRST live plan, persists run id + exact plan hash, exits PAUSED (exit 4)
    2) authenticated approver reviews the plan (workspace UI with QA_DATA_DIR=reports/eval/data, or report files) and runs
         QA_TOKEN=<approver token> python -m qa_agent.eval_authorization review --data-dir reports/eval/data \\
             --users-file <users.json> --run <run_id> --plan-hash <hash> --decision approved|rejected
       and creates a server-side grant (POST /api/eval-grants or `python -m qa_agent.eval_authorization create ...`)
    3) python scripts/eval_b2.py --provider <approved-provider> --confirm-live-approved --out reports/eval --grant <grant_id> \\
             --sealed-gold-dir <custody dir>        -> resumes after verifying the approval; never auto-approves the first plan

* replay/stub = HARNESS SELF-TEST: hand-authored recordings, NOT model reasoning. Held-out stories are skipped there.
* A full evaluation needs: every required story, >= min_runs_per_story runs, a defect-ON and a defect-OFF phase with
  execution results for every run, and (live) the sealed gold for held-out stories. Anything less is never PASS.
* Failed/missing runs stay in every denominator (scored as zero / not detected / incomplete).
* Execution needs approval. The FIRST plan is always human-reviewed (pause/resume, state survives restarts). Later runs
  use QAAgent.approve_unattended with a server-side grant created by an authenticated approver (scoped, use-limited,
  revocable, audited). Without a grant, runs stop at awaiting_approval (=> NOT_EVALUABLE).
* Eval-only overrides come from config/eval_profile.json (e.g. max_retries=1); the global runtime default is unchanged.
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


def eval_profile_overrides(cfg):
    prof = load_json(ROOT / "config/eval_profile.json")
    for k, v in prof.get("providers", {}).items():
        cfg["providers"].setdefault(k, {}).update(v)
    cfg["budget"] = {**cfg.get("budget", {}), **prof.get("budget", {})}
    return cfg


def agent_config(provider, stub_url=None):
    cfg = eval_profile_overrides(copy.deepcopy(load_json(Path(os.environ.get("QA_AGENT_CONFIG", ROOT / "config/agent.json")))))
    if provider == "replay":
        cfg["model"]["provider"] = "replay"
    elif provider == "stub":
        cfg["model"]["provider"] = "compatible"
        cfg["providers"]["compatible"].update(enabled=True, base_url=stub_url + "/v1", pricing=dict(STUB_PRICING), timeout_s=30)
        cfg["budget"].update(scope="stub-selftest", total_usd=1000.0, daily_usd=1000.0)   # FAKE prices; isolated scope
    elif provider not in ("stub", "replay"):
        cfg["model"]["provider"] = provider
        host = urlparse(cfg["providers"][provider].get("base_url", "")).hostname or ""
        if provider == "compatible" and host in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit("refusing: --provider <approved-provider> points at a loopback/stub endpoint; live eval must not use recorded answers")
    if provider in ("stub", "replay"):
        cfg.setdefault("unattended_eval_approval", {})["enabled"] = True   # self-test only; still needs a server-side grant
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


def run_story(agent, key, gold, phase, execute, grant, recorded, provider, first=None, requested_by=None):
    t0 = time.monotonic()
    if first:   # the human-reviewed first plan, planned in an earlier invocation
        rid, latency = first["run_id"], first.get("latency_s", 0.0)
    else:
        rid = agent.start_run(f"Plan {key}", story_key=key, wait=True, requested_by=requested_by)
        latency = round(time.monotonic() - t0, 3)
    run = agent.store.get_run(rid)
    kinds = [t["kind"] for t in agent.store.q("SELECT kind FROM telemetry WHERE run_id=?", (rid,))]
    usage = agent.store.run_usage(rid)
    priced = [c for c in usage["calls"] if c.get("priced")]
    rec = {"run_id": rid, "story": key, "phase": phase, "status": run["status"], "error": run.get("error"),
           "model_label": run.get("model_label"), "model_provider": run.get("model_provider"), "model_id": run.get("model_id"), "model_version": run.get("model_version"), "latency_s": latency,
           "policy_violations": kinds.count("policy_violation"), "format_errors": kinds.count("format_error"),
           "schema_valid_first_try": (run["status"] == "awaiting_approval" or bool(first)) and kinds.count("format_error") == 0,
           "tokens_total_actual": sum(c["total_tokens"] or 0 for c in priced),
           "cost_usd_actual": round(sum(c["cost_usd"] or 0 for c in priced), 6), "unpriced_calls": len(usage["calls"]) - len(priced),
           "planned": run["status"] == "awaiting_approval" or bool(first), "execution": None, "execution_missing_reason": None,
           "first_plan_human_reviewed": bool(first)}
    if not rec["planned"]:
        rec["quality"] = dict(ZERO_Q, injection_flagged=False if gold.get("injection") else None, contaminated=False, near_copy=None)
        rec["execution_missing_reason"] = "planning failed"
        return rec
    plan = run["planning_json"]
    oracles = load_json(ROOT / f"fixtures/oracles/{key}.expected.json")
    q = score_plan(plan, gold, oracles, recorded if provider not in ("stub", "replay") else None)
    q["near_copy"] = near_copy_similarity(plan, recorded) if (recorded is not None and provider not in ("stub", "replay")) else None
    rec["quality"] = q
    if not execute:
        rec["execution_missing_reason"] = "plan-only (smoke)"
        return rec
    from qa_agent.eval_authorization import AuthorizationError
    if first:
        if run["status"] == "approved":
            agent.execute_approved(rid, wait=True)
    elif not grant:
        rec["execution_missing_reason"] = "awaiting human approval (no --grant)"
        return rec
    else:
        try:
            agent.approve_unattended(rid, grant, wait=True)
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
        "contaminated_plans": sum(1 for x in q if x.get("contaminated")) if provider not in ("stub", "replay") else 0,
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
                 f"{'n/a (missing)' if v['pass'] is None else ('PASS' if v['pass'] else 'FAIL')} |")
    L += ["", f"Seeded-defect recall (defect-ON): {a['seeded_defects_detected']}/{a['seeded_defects_expected']} · "
              f"false alarms (defect-OFF): {a['false_alarms']} · incomplete defect-OFF runs: {a['defect_off_runs_incomplete']} · "
              f"planning failures: {a['planning_failures']}/{a['runs']}",
          f"Near-copy similarity (heuristic, not gated): max {a['near_copy_max']}, ≥{NEAR_COPY_THRESHOLD}: {a['near_copy_over_threshold']}",
          f"Tokens (actual): {a['tokens_total_actual']} · cost (actual usage × configured price): ${a['cost_usd_actual']} · unpriced calls: {a['unpriced_calls']}", ""]
    return "\n".join(L)


STATE = "eval_state.json"


def _selftest_principal(out):
    from qa_agent.auth import UserStore
    us = UserStore(out / "selftest_users.json")
    tok = us.add("selftest_reviewer", "approver") if "selftest_reviewer" not in json.loads((out / "selftest_users.json").read_text() if (out / "selftest_users.json").exists() else '{"users":{}}').get("users", {}) else None
    if tok is None:   # rotate: tokens are only shown once
        d = json.loads((out / "selftest_users.json").read_text()); d["users"].pop("selftest_reviewer"); (out / "selftest_users.json").write_text(json.dumps(d))
        tok = us.add("selftest_reviewer", "approver")
    return us.verify(tok)


def selftest_grant(agent, out, stories):
    from qa_agent.eval_authorization import create_grant
    from datetime import datetime, timedelta, timezone
    g = create_grant(agent.store, agent.gateway, _selftest_principal(out), {
        "stories": stories, "environment": "synthetic-local", "skills": ["pim_import_testing", "attribute_mapping_validation"],
        "actions": ["api_import", "ui_import_error", "ui_import_and_count", "api_product_after_import"],
        "model_classes": ["STUB ENDPOINT", "REPLAYED"], "max_uses": 1000,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()})
    return g["grant_id"]


def first_plan_stage(agent, a, out, stories, simulate):
    """Plan the first plan, persist (run id + exact plan hash), pause for an authenticated human review; resume only after
    a verified manual approval of that exact plan. Returns (first, paused)."""
    sp = out / STATE
    st = json.loads(sp.read_text()) if sp.exists() else {}
    f = st.get("first_plan")
    if not f:
        key = stories[0]
        t0 = time.monotonic()
        rid = agent.start_run(f"Plan {key}", story_key=key, wait=True, requested_by=getattr(a, "requested_by", None))
        run = agent.store.get_run(rid)
        f = {"run_id": rid, "story": key, "plan_hash": run["plan_hash"], "latency_s": round(time.monotonic() - t0, 3),
             "model_label": run["model_label"], "status_at_creation": run["status"]}
        st["first_plan"] = f; sp.write_text(json.dumps(st, indent=2))
        if run["status"] != "awaiting_approval":
            return None, {"verdict": "NOT_EVALUABLE", "exit": 3, "reason": f"first plan failed: {run.get('error')}", **f}
        if simulate:
            agent.review_plan(rid, _selftest_principal(out), "approved", run["plan_hash"], "SIMULATED reviewer (stub self-test only)")
    run = agent.store.get_run(f["run_id"])
    if run and (run.get("model_provider") != agent.model.name or run.get("model_id") != getattr(agent.model, "model", agent.model.name)):
        return None, {"verdict": "NOT_EVALUABLE", "exit": 3, "reason": "first-plan provider/model changed; use a separate evaluation output directory", **f}
    if not run:
        return None, {"verdict": "NOT_EVALUABLE", "exit": 3, "reason": "first-plan run missing from the data dir (state/DB mismatch)", **f}
    if getattr(a, "requested_by", None) and run.get("requested_by") != a.requested_by:
        return None, {"verdict": "NOT_EVALUABLE", "exit": 3, "reason": "first-plan requester changed; original authenticated requester required", **f}
    if getattr(agent.model, "version", None) and run.get("model_version") != agent.model.version:
        return None, {"verdict": "NOT_EVALUABLE", "exit": 3, "reason": "first-plan model version changed; use a fresh evaluation directory", **f}
    if run["status"] == "awaiting_approval":
        return None, {"verdict": "PAUSED/AWAITING_FIRST_PLAN_APPROVAL", "exit": 4, **f,
                      "reason": "first live plan requires manual review and approval by an authenticated approver",
                      "next": f"QA_TOKEN=<approver token> python -m qa_agent.eval_authorization review --data-dir {out / 'data'} "
                              f"--users-file <users.json> --run {f['run_id']} --plan-hash {f['plan_hash']} --decision approved|rejected"}
    ap = agent.approvals.latest(f["run_id"])
    if run["status"] == "rejected" or (ap and ap["decision"] == "rejected"):
        return None, {"verdict": "NOT_EVALUABLE", "exit": 5, "reason": f"first plan rejected by {ap and ap['approver']}", **f}
    if not ap or ap["decision"] != "approved" or not ap.get("identity") or ap.get("auth_mode") != "local":
        return None, {"verdict": "NOT_EVALUABLE", "exit": 3, "reason": "first plan has no manual authenticated approval", **f}
    if not (ap["plan_hash"] == run["plan_hash"] == f["plan_hash"]) or run["plan_hash"] != _sha_plan(run):
        return None, {"verdict": "NOT_EVALUABLE", "exit": 3, "reason": "first plan altered: approved hash != stored plan", **f}
    f = {**f, "approved_by": ap["identity"], "approved_at": ap["ts"]}
    return f, None


def _sha_plan(run):
    from qa_agent.execution_package import digest, PackageChanged
    try:
        return digest(run)
    except PackageChanged:
        return None


def pinned_identity_matches(runs, provider, model, version):
    """B3 pins an observed digest; never combine revisions into a complete PASS."""
    return all(r.get("model_provider") == provider and r.get("model_id") == model
               and (not version or r.get("model_version") == version) for r in runs if r.get("planned"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["replay", "stub", "compatible", "ollama", "lmstudio", "llamacpp", "vllm", "groq", "gemini", "openrouter"], required=True)
    ap.add_argument("--model", help="exact administrator-approved model ID")
    ap.add_argument("--runs", type=int)
    ap.add_argument("--stories")
    ap.add_argument("--out", default="reports/eval")
    ap.add_argument("--no-execute", action="store_true")
    ap.add_argument("--grant", help="server-side eval grant id (created by an authenticated approver)")
    ap.add_argument("--self-test-simulated-review", action="store_true",
                    help="stub/replay ONLY: a SIMULATED local approver reviews the first plan and creates the grant")
    ap.add_argument("--sealed-gold-dir")
    ap.add_argument("--confirm-live-approved", action="store_true")
    ap.add_argument("--requester-users-file", help="optional local requester authentication via QA_TOKEN; required by B3 wrapper")
    a = ap.parse_args(argv)
    a.requested_by = None
    if a.requester_users_file:
        from qa_agent.auth import UserStore
        principal = UserStore(Path(a.requester_users_file)).verify(os.environ.get("QA_TOKEN", ""))
        if not principal or not principal.authenticated or principal.role != "requester":
            raise SystemExit("refusing: QA_TOKEN must authenticate a local requester")
        if a.provider in ("stub", "replay"):
            raise SystemExit("refusing: authenticated B3 requester option is for real providers only")
        a.requested_by = principal.name
    if a.provider not in ("stub", "replay") and not a.confirm_live_approved:
        raise SystemExit("refusing: live eval needs --confirm-live-approved (see docs/b2/LIVE_EVAL_APPROVAL_REQUEST.md)")
    if a.self_test_simulated_review and a.provider not in ("stub", "replay"):
        raise SystemExit("refusing: simulated review is for stub/replay self-tests only; live first plans need a real approver")
    from qa_agent.runtime import QAAgent
    saved = {k: os.environ.get(k) for k in ("SYNTHETIC_SUT_URL", "MODEL_API_KEY")}
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
            if a.provider in ("stub", "replay"):
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
            os.environ.setdefault("MODEL_API_KEY", "stub-not-a-real-key-000000")
        cfg = agent_config(a.provider, stub)
        if getattr(a, "requested_by", None) and cfg.get("auth", {}).get("mode") != "local":
            raise SystemExit("refusing: authenticated requester evaluation requires auth.mode=local")
        if a.model:
            pc = cfg["providers"].get(a.provider, {})
            if pc.get("model") != a.model:
                entry = next((m for m in pc.get("models", []) if m.get("id") == a.model and m.get("approved") is True), None)
                if not entry:
                    raise SystemExit("refusing: requested model not approved by administrator")
                pc.update({("model" if k == "id" else k): v for k,v in entry.items() if k in ("id", "version", "context_window", "structured_output", "max_completion_tokens")})
        try:
            agent = QAAgent(out / "data", agent_cfg=cfg)
        except Exception as e:   # fail closed (compatible disabled / unapproved / unpriced / caps missing)
            rep = {"provider": a.provider, "error": f"{type(e).__name__}: {e}", "verdict": "NOT_EVALUABLE",
                   "mode": "not-run", "completeness": {"complete": False, "missing": ["provider construction failed (fail-closed)"]}}
            (out / "eval_report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8"); print(json.dumps(rep, indent=2)); return 2
        runs, first = [], None
        if not a.no_execute:
            first, paused = first_plan_stage(agent, a, out, [k for k in golds], a.self_test_simulated_review)
            if paused:
                rep = {"provider": a.provider, "verdict": paused["verdict"], "mode": "paused", "first_plan": paused,
                       "completeness": {"complete": False, "missing": [paused["reason"]]}}
                (out / "eval_report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8"); print(json.dumps(rep, indent=2))
                return paused["exit"]
            if a.self_test_simulated_review and not a.grant:
                a.grant = selftest_grant(agent, out, list(golds))
        for phase in phases:
            defects = "" if phase == "plan_only" else es["phases"][phase]
            with serve("sut.app:app", {"SUT_DEFECTS": defects}, "/api/health") as sut:
                os.environ["SYNTHETIC_SUT_URL"] = sut
                for k in golds:
                    rec = load_json(ROOT / f"fixtures/replay/{k}.json")["responses"]["plan_story"] if (ROOT / f"fixtures/replay/{k}.json").exists() else None
                    for i in range(runs_n):
                        use_first = first if (first and phase == "defect_on" and k == first["story"] and i == 0) else None
                        runs.append(run_story(agent, k, golds[k], phase, not a.no_execute, a.grant, rec, a.provider, use_first,
                                              requested_by=getattr(a, "requested_by", None)))
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
    if a.provider not in ("stub", "replay") and not pinned_identity_matches(runs, a.provider, cfg["providers"][a.provider]["model"], cfg["providers"][a.provider].get("version")):
        missing.append("provider/model/pinned version changed across evaluation runs")
    agg = aggregate(runs, golds, a.provider)
    th = check_thresholds(agg)
    completeness = {"mode": "smoke" if smoke else "full", "complete": not missing, "missing": missing}
    v = verdict(agg, th, completeness)
    real_evidence = a.provider not in ("stub", "replay") and not agg.get("contaminated_plans") and all((r.get("model_label") or "").startswith("LIVE MODEL (") for r in runs) and bool(runs)
    if a.provider not in ("stub", "replay") and not real_evidence: v = "NOT_EVALUABLE"
    banner = ("HARNESS SELF-TEST — answers are hand-authored STUB/REPLAY recordings, NOT real model reasoning. Cost uses FAKE stub pricing."
              + (" First-plan review and grant were SIMULATED (self-test only)." if a.self_test_simulated_review else "")
              if a.provider in ("stub", "replay") else "LIVE MODEL evaluation on synthetic data. Cost from actual usage × configured price.")
    rep = {"provider": a.provider, "model_id": agent.model.model, "model_version": getattr(agent.model, "version", None), "real_reasoning_evidence": real_evidence, "banner": banner, "mode": completeness["mode"], "verdict": v, "stories": list(golds),
           "runs_per_story": runs_n, "phases": phases, "completeness": completeness, "held_out": held_out_info,
           "proposed_thresholds": {k: list(x) for k, x in PROPOSED_THRESHOLDS.items()}, "aggregate": agg, "thresholds": th,
           "first_plan": first, "simulated_review": bool(a.self_test_simulated_review),
           "runs": runs, "model_labels": sorted({r.get("model_label") or "" for r in runs})}
    agent.store.telemetry(None, "model_evaluation", agent.model.name, json.dumps({"model": agent.model.model, "verdict": v, "score": agg, "real_reasoning_evidence": real_evidence}))
    (out / "eval_report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    (out / "eval_report.md").write_text(markdown(rep), encoding="utf-8")
    print(markdown(rep))
    return {"PASS": 0, "FAIL": 1}.get(v, 3)


if __name__ == "__main__":
    sys.exit(main())
