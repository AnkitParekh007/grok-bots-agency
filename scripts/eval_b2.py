#!/usr/bin/env python3
"""B2 offline plan-quality evaluation harness.

  python scripts/eval_b2.py --provider stub   [--runs 3] [--out reports/eval] [--no-execute] [--stories PCC-DEMO-5,...]
  python scripts/eval_b2.py --provider replay ...
  python scripts/eval_b2.py --provider grok --confirm-live-approved   # ONLY after the B2 approval pack is signed

replay/stub modes are a HARNESS SELF-TEST: answers are hand-authored recordings, NOT model reasoning.
grok mode uses config/agent.json unchanged (disabled by default => fails closed), refuses loopback/stub endpoints and
flags any plan identical to a recorded stub answer as contaminated. Cost is reported from actual usage only.
Execution approvals in this harness are automatic and labelled 'eval-harness (synthetic eval only)'.
"""
import argparse, contextlib, copy, json, os, socket, subprocess, sys, time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx  # noqa: E402
from qa_agent.eval_metrics import (score_plan, detection, repeatability, p95, check_thresholds, PROPOSED_THRESHOLDS)  # noqa: E402
from qa_agent.util import load_json  # noqa: E402

STUB_PRICING = {"input_per_mtok_usd": 1.0, "cached_input_per_mtok_usd": 0.25, "output_per_mtok_usd": 5.0}  # FAKE, stub only


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
        g = cfg["providers"]["grok"]
        g.update(enabled=True, base_url=stub_url + "/v1", pricing=dict(STUB_PRICING), timeout_s=30, max_retries=1)
    elif provider == "grok":
        cfg["model"]["provider"] = "grok"
        host = urlparse(cfg["providers"]["grok"].get("base_url", "")).hostname or ""
        if host in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit("refusing: --provider grok points at a loopback/stub endpoint; live eval must not use recorded answers")
    return cfg


def run_story(agent, key, gold, execute, recorded):
    from qa_agent.planning import PolicyViolation  # noqa: F401
    t0 = time.monotonic()
    rid = agent.start_run(f"Plan {key}", story_key=key, wait=True)
    latency = round(time.monotonic() - t0, 3)
    run = agent.store.get_run(rid)
    tel = agent.store.q("SELECT kind FROM telemetry WHERE run_id=?", (rid,))
    kinds = [t["kind"] for t in tel]
    usage = agent.store.run_usage(rid)
    priced = [c for c in usage["calls"] if c.get("priced")]
    rec = {"run_id": rid, "status": run["status"], "error": run.get("error"), "model_label": run.get("model_label"),
           "latency_s": latency, "policy_violations": kinds.count("policy_violation"), "format_errors": kinds.count("format_error"),
           "schema_valid_first_try": run["status"] == "awaiting_approval" and kinds.count("format_error") == 0,
           "tokens_total_actual": sum(c["total_tokens"] or 0 for c in priced),
           "cost_usd_actual": round(sum(c["cost_usd"] or 0 for c in priced), 6),
           "unpriced_calls": len(usage["calls"]) - len(priced)}
    if run["status"] != "awaiting_approval":
        return rec
    plan = run["planning_json"]
    oracles = load_json(ROOT / f"fixtures/oracles/{key}.expected.json")
    rec["quality"] = score_plan(plan, gold, oracles, recorded)
    if execute:
        agent.approve(rid, "eval-harness (synthetic eval only)", "approved", wait=True)
        rec["execution"] = {"status": agent.store.get_run(rid)["status"], **detection(plan, agent.store.results(rid), gold)}
    return rec


def aggregate(per_story, provider):
    runs = [r for s in per_story.values() for r in s["runs"]]
    q = [r["quality"] for r in runs if "quality" in r]
    ex = [r["execution"] for r in runs if "execution" in r]
    mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else None
    det = [e["seeded_defect_detection"] for e in ex if e["seeded_defect_detection"] is not None]
    inj = [x["injection_flagged"] for x in q if x["injection_flagged"] is not None]
    agg = {
        "runs": len(runs), "planned_ok": len(q),
        "ac_precision": mean([x["ac_precision"] for x in q]), "ac_recall": mean([x["ac_recall"] for x in q]),
        "required_coverage": mean([x["required_coverage"] for x in q]),
        "required_categories_present": mean([x["required_categories_present"] for x in q]),
        "skill_action_correct_rate": mean([x["skill_action_correct_rate"] for x in q]),
        "gap_recall": mean([x["gap_recall"] for x in q]), "gap_precision": mean([x["gap_precision"] for x in q]),
        "injection_flagged_rate": mean([float(x) for x in inj]),
        "policy_violations": sum(r["policy_violations"] for r in runs),
        "schema_valid_first_try_rate": mean([float(r["schema_valid_first_try"]) for r in runs]),
        "mean_repairs": mean([r["format_errors"] for r in runs]),
        "seeded_defect_detection": mean(det), "false_alarms": sum(len(e["false_alarms"]) for e in ex) if ex else None,
        "execution_errors": sum(e["execution_errors"] for e in ex) if ex else None,
        "repeatability": mean([s["repeatability"] for s in per_story.values() if s["repeatability"] is not None]),
        "plan_latency_p95_s": p95([r["latency_s"] for r in runs]),
        "contaminated_plans": sum(1 for x in q if x["contaminated"]) if provider == "grok" else 0,
        "tokens_total_actual": sum(r["tokens_total_actual"] for r in runs),
        "cost_usd_actual": round(sum(r["cost_usd_actual"] for r in runs), 6),
        "unpriced_calls": sum(r["unpriced_calls"] for r in runs),
    }
    return agg


def markdown(report):
    a, th = report["aggregate"], report["thresholds"]
    L = [f"# B2 plan-quality evaluation — provider `{report['provider']}`", "",
         f"> {report['banner']}", "",
         f"Stories: {', '.join(report['stories'])} · runs per story: {report['runs_per_story']} · execute: {report['execute']} · SUT defects: `{report['sut_defects']}`", "",
         "## Metrics vs PROPOSED thresholds", "", "| metric | value | proposed threshold | hard gate | pass |", "|---|---|---|---|---|"]
    for k, v in th.items():
        L.append(f"| {k} | {v['value']} | {v['threshold']} | {'yes' if v['hard_gate'] else ''} | {'' if v['pass'] is None else ('✅' if v['pass'] else '❌')} |")
    L += ["", f"Tokens (actual usage): {a['tokens_total_actual']} · cost (actual usage × configured price): ${a['cost_usd_actual']} · unpriced calls: {a['unpriced_calls']}", "",
          "## Per story", "", "| story | runs ok | AC P/R | coverage | gaps R | inj. flagged | defects detected | false alarms | repeatability |", "|---|---|---|---|---|---|---|---|---|"]
    for k, s in report["per_story"].items():
        q = [r["quality"] for r in s["runs"] if "quality" in r]
        e = [r["execution"] for r in s["runs"] if "execution" in r]
        f = lambda xs: "-" if not xs else str(round(sum(xs) / len(xs), 3))
        L.append(f"| {k} | {len(q)}/{len(s['runs'])} | {f([x['ac_precision'] for x in q])}/{f([x['ac_recall'] for x in q])} | "
                 f"{f([x['required_coverage'] for x in q])} | {f([x['gap_recall'] for x in q])} | "
                 f"{'-' if not q or q[0]['injection_flagged'] is None else all(x['injection_flagged'] for x in q)} | "
                 f"{'-' if not e else ', '.join(sorted({o for x in e for o in x['detected']})) or 'none'} | "
                 f"{'-' if not e else sum(len(x['false_alarms']) for x in e)} | {s['repeatability']} |")
    L += ["", f"Overall verdict (proposed thresholds): **{report['verdict']}**", ""]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["replay", "stub", "grok"], required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--stories")
    ap.add_argument("--out", default="reports/eval")
    ap.add_argument("--no-execute", action="store_true")
    ap.add_argument("--confirm-live-approved", action="store_true")
    a = ap.parse_args(argv)
    es = load_json(ROOT / "fixtures/eval/eval_set.json")
    stories = a.stories.split(",") if a.stories else es["stories"]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    if a.provider == "grok" and not a.confirm_live_approved:
        raise SystemExit("refusing: live eval needs --confirm-live-approved (see docs/b2/LIVE_EVAL_APPROVAL_REQUEST.md)")
    from qa_agent.runtime import QAAgent
    saved = {k: os.environ.get(k) for k in ("SYNTHETIC_SUT_URL", "XAI_API_KEY")}
    try:
        return _run(a, es, stories, out, QAAgent)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run(a, es, stories, out, QAAgent):
    with contextlib.ExitStack() as st:
        sut = st.enter_context(serve("sut.app:app", {"SUT_DEFECTS": es["sut_defects_for_detection"]}, "/api/health"))
        os.environ["SYNTHETIC_SUT_URL"] = sut
        stub = None
        if a.provider == "stub":
            stub = st.enter_context(serve("qa_agent.devtools.model_stub:app", probe="/_stub/requests"))
            os.environ.setdefault("XAI_API_KEY", "stub-not-a-real-key-000000")
        cfg = agent_config(a.provider, stub)
        try:
            agent = QAAgent(out / "data", agent_cfg=cfg)
        except Exception as e:   # fail closed (e.g. grok disabled / unapproved / unpriced)
            rep = {"provider": a.provider, "error": f"{type(e).__name__}: {e}", "verdict": "NOT RUN (fail-closed)"}
            (out / "eval_report.json").write_text(json.dumps(rep, indent=2))
            print(json.dumps(rep, indent=2)); return 2
        per = {}
        for k in stories:
            gold = load_json(ROOT / f"fixtures/eval/gold/{k}.gold.json")
            recorded = load_json(ROOT / f"fixtures/replay/{k}.json")["responses"]["plan_story"] if a.provider == "grok" else None
            runs = [run_story(agent, k, gold, not a.no_execute, recorded) for _ in range(a.runs)]
            per[k] = {"runs": runs, "repeatability": repeatability([r["quality"]["test_signatures"] for r in runs if "quality" in r])}
    agg = aggregate(per, a.provider)
    th = check_thresholds(agg)
    hard_fail = any(v["hard_gate"] and v["pass"] is False for v in th.values())
    verdict = "FAIL (hard gate)" if hard_fail else ("PASS" if all(v["pass"] in (True, None) for v in th.values()) else "BELOW PROPOSED THRESHOLDS")
    banner = ("HARNESS SELF-TEST — answers are hand-authored STUB/REPLAY recordings, NOT real model reasoning. Cost uses FAKE stub pricing."
              if a.provider in ("stub", "replay") else "LIVE MODEL evaluation on synthetic data. Cost from actual usage × configured price.")
    report = {"provider": a.provider, "banner": banner, "stories": stories, "runs_per_story": a.runs, "execute": not a.no_execute,
              "sut_defects": es["sut_defects_for_detection"], "proposed_thresholds": {k: list(v) for k, v in PROPOSED_THRESHOLDS.items()},
              "aggregate": agg, "thresholds": th, "verdict": verdict, "per_story": per,
              "model_labels": sorted({r.get("model_label") or "" for s in per.values() for r in s["runs"]})}
    (out / "eval_report.json").write_text(json.dumps(report, indent=2))
    (out / "eval_report.md").write_text(markdown(report))
    print(markdown(report))
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
