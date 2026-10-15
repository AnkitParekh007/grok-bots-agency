"""B3 local diagnostics and authenticated smoke handoff; scoring/benchmarking reuses B2.

No installers, model pulls, hosted providers, replay substitution or approval creation.
"""
import argparse
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qa_agent.auth import UserStore  # noqa: E402
from qa_agent.audit_verify import verify_data_dir  # noqa: E402
from qa_agent.devtools.local_diagnostics import hardware, memory_sample, gpu_sample, ollama_metadata  # noqa: E402
from qa_agent.environments import _is_loopback  # noqa: E402
from qa_agent.gateway import PolicyDenied  # noqa: E402
from qa_agent.providers.models import ProviderNotApproved  # noqa: E402
from qa_agent.runtime import QAAgent  # noqa: E402
from qa_agent import planning as P  # noqa: E402
from qa_agent.util import canon, load_json, now, redact  # noqa: E402
from scripts import eval_b2 as B2  # noqa: E402


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True), encoding="utf-8")


def configuration():
    path = Path(os.environ.get("QA_AGENT_CONFIG", ROOT / "config/agent.json"))
    cfg = B2.eval_profile_overrides(copy.deepcopy(load_json(path)))
    p = cfg["providers"]["ollama"]
    u = urlparse(p.get("base_url", ""))
    if not _is_loopback(u.hostname) or u.scheme != "http" or u.path not in ("", "/") or u.username or u.password or u.query or u.fragment:
        raise ValueError("B3 requires a plain loopback native Ollama endpoint")
    if not p.get("model") or ":cloud" in p["model"].lower():
        raise ValueError("B3 requires an exact installed local model, never a cloud tag")
    if p.get("api_key_env") is not None:
        raise ValueError("B3 loopback Ollama does not accept a hosted credential reference")
    if cfg.get("issue_tracker", {}).get("provider") != "mock_jira" or cfg.get("git", {}).get("provider") != "synthetic_git" or cfg.get("default_environment") != "synthetic-local":
        raise ValueError("B3 permits only fixture integrations and synthetic-local")
    if cfg["issue_tracker"].get("fixtures_dir") != "fixtures/jira" or cfg["git"].get("repos_dir") != "fixtures/repos" or cfg.get("oracles_dir") != "fixtures/oracles" or cfg.get("fixture_data_dir") != "fixtures/data":
        raise ValueError("B3 requires the repository's synthetic fixture directories")
    cfg["model"]["provider"] = "ollama"  # explicit, never QA_MODEL_PROVIDER or a hosted default
    return cfg


def requester(cfg, users_file):
    if cfg.get("auth", {}).get("mode") != "local":
        raise ValueError("set auth.mode=local in the untracked administrator config")
    p = UserStore(Path(users_file)).verify(os.environ.get("QA_TOKEN", ""))
    if not p or not p.authenticated or p.role != "requester":
        raise ValueError("QA_TOKEN must authenticate a local requester; a distinct human approver is required")
    return p


def identity(cfg):
    p = cfg["providers"]["ollama"]
    fields = ("model", "base_url", "context_window", "max_completion_tokens", "timeout_s", "max_retries", "structured_output")
    settings = {k: p.get(k) for k in fields}
    bound = {k: v for k, v in cfg.items() if k != "unattended_eval_approval"}
    return {"settings": settings, "settings_sha256": hashlib.sha256(canon(settings).encode()).hexdigest(),
            "configuration_sha256": hashlib.sha256(canon(bound).encode()).hexdigest(),
            "configuration_scope": "excludes separately authorized eval-grant enablement; grants remain independently authenticated/scoped",
            "native_request": {"stream": False, "num_ctx": p["context_window"], "num_predict": p["max_completion_tokens"],
                               "thinking_temperature_seed": "not specified; runtime/model defaults"}}


def source_hashes():
    paths = ("scripts/local_model_b3.py", "scripts/eval_b2.py", "qa_agent/runtime.py", "qa_agent/planning.py",
             "qa_agent/contracts.py", "qa_agent/providers/local.py", "qa_agent/providers/models.py",
             "qa_agent/gateway.py", "qa_agent/executor.py", "qa_agent/comparator.py", "qa_agent/skills/__init__.py",
             "qa_agent/skills/pim_import_testing.py", "qa_agent/skills/attribute_mapping_validation.py",
             "config/policy.json", "config/environments.json", "config/eval_profile.json",
             "fixtures/jira/PCC-DEMO-5.json", "fixtures/oracles/PCC-DEMO-5.expected.json")
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}


@contextlib.contextmanager
def sampling(out):
    stop = threading.Event(); samples = []
    path = out / f"telemetry-{time.time_ns()}.json"
    def collect():
        while not stop.is_set():
            samples.append({"at": now(), "system_ram": memory_sample(), "nvidia": gpu_sample()})
            stop.wait(2)
    worker = threading.Thread(target=collect, daemon=True); worker.start()
    try:
        yield {"file": path.name, "samples": samples}
    finally:
        stop.set(); worker.join(timeout=10)
        write(path, {"scope": "system RAM / NVIDIA device, not model-process RAM; Intel VRAM unavailable", "samples": samples})


def diagnose(cfg, out, machine=True):
    agent = QAAgent(out / "data", agent_cfg=cfg)
    story = load_json(ROOT / "fixtures/jira/PCC-DEMO-5.json")
    ctx = {"story_key": "PCC-DEMO-5", "story": story, "data_class": "synthetic",
           "catalog": P.build_catalog(agent.skills, agent._oracles("PCC-DEMO-5"), agent.fixture_files())}
    body = agent.model.build_body("plan_story", ctx, P.PLAN_STORY_SCHEMA)
    ceiling = len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + 256 + agent.model.max_completion_tokens
    meta = {"observed_at": now(), "hardware": hardware() if machine else None,
            "inference": ollama_metadata(agent.model), "generation": identity(cfg),
            "source_sha256": source_hashes(),
            "working_tree_dirty": subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode != 0,
            "context_preflight": {"conservative_input_plus_output_ceiling": ceiling, "fits_configured_context": ceiling <= agent.model.context_window,
                                  "prompt_sha256": hashlib.sha256(canon(body).encode()).hexdigest()},
            "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip() or None}
    if ceiling > agent.model.context_window:
        meta["inference"].update(ready=False, error="complete synthetic prompt/catalog exceeds configured context")
    write(out / f"diagnostics-{time.time_ns()}.json", meta)
    return agent, meta


def plan(cfg, out, users_file, phase):
    who = requester(cfg, users_file)
    if (out / "b3_attempt.json").exists():
        raise ValueError("attempt directory already used; summarize it or choose a fresh directory")
    agent, meta = diagnose(cfg, out)
    if not meta["inference"]["ready"]:
        result = {"verdict": "NOT_EVALUABLE", "stage": "preflight", "real_reasoning_evidence": False,
                  "reason": meta["inference"].get("error"), "metadata": meta, "run_id": None}
        write(out / "b3_summary.json", result); return 3, result
    t0 = time.monotonic()
    with sampling(out) as telemetry:
        rid = agent.start_run("Plan PCC-DEMO-5", story_key="PCC-DEMO-5", environment="synthetic-local", requested_by=who.name, wait=True)
    run = agent.store.get_run(rid)
    attempt = {"run_id": rid, "story": "PCC-DEMO-5", "phase": phase, "requester": who.name,
               "plan_hash": run["plan_hash"], "latency_s": round(time.monotonic()-t0, 3), "metadata": meta,
               "telemetry_file": telemetry["file"] if telemetry else None}
    write(out / "b3_attempt.json", attempt)
    agent.gateway.record(rid, "b3.smoke_context", {"context_hash": hashlib.sha256(canon(attempt).encode()).hexdigest()}, "allow", "phase and model metadata bound before human review")
    write(out / "review_plan.json", {"run_id": rid, "phase": phase, "requester": who.name,
                                     "plan_hash": run["plan_hash"], "story": run["story_json"],
                                     "planning": run["planning_json"], "validation_status": run["status"]})
    result = summarize(cfg, out)
    return (4 if run["status"] == "awaiting_approval" else 3), result


def evidence_status(base, results):
    """Verify stable test artifacts against the report manifest, excluding growing audit logs."""
    base = base.resolve()
    try:
        raw = (base / "report.json").read_bytes()
        checksum = (base / "report.json.sha256").read_text().split()[0]
        report = json.loads(raw)
        hashes = {r["path"].replace("\\", "/"): r["sha256"] for r in report["artifacts"]}
        ok = hashlib.sha256(raw).hexdigest() == checksum and (base / "report.html").is_file() and bool(results)
        for r in results:
            for field in ("screenshot", "trace", "log"):
                rel = r.get(field)
                if not rel:
                    ok = False; continue
                path = (base / rel).resolve()
                if not path.is_relative_to(base) or not path.is_file() or path.stat().st_size == 0:
                    ok = False; continue
                ok = ok and hashes.get(rel.replace("\\", "/")) == hashlib.sha256(path.read_bytes()).hexdigest()
        return {"ok": ok, "json_checksum_ok": hashlib.sha256(raw).hexdigest() == checksum,
                "screenshots": sum(bool(r.get("screenshot")) for r in results), "traces": sum(bool(r.get("trace")) for r in results)}
    except (OSError, ValueError, KeyError, IndexError):
        return {"ok": False, "json_checksum_ok": False, "screenshots": 0, "traces": 0}


def summarize(cfg, out):
    a = load_json(out / "b3_attempt.json")
    agent = QAAgent(out / "data", agent_cfg=cfg)
    run = agent.store.get_run(a["run_id"])
    expected = hashlib.sha256(canon(a).encode()).hexdigest()
    records = agent.store.q("SELECT args_summary FROM audit WHERE run_id=? AND tool='b3.smoke_context' AND decision='allow'", (a["run_id"],))
    if not records or not any(expected in r["args_summary"] for r in records):
        raise ValueError("smoke context metadata has no matching audited binding")
    if not run or run["model_provider"] != "ollama" or run["model_id"] != a["metadata"]["inference"]["model"] or run["plan_hash"] != a["plan_hash"]:
        raise ValueError("run/model/plan changed from original smoke context")
    approval = agent.approvals.latest(a["run_id"])
    reviewed = bool(approval and approval["decision"] == "approved" and approval.get("auth_mode") == "local"
                    and approval.get("identity") and approval["identity"] != a["requester"] and approval["plan_hash"] == run["plan_hash"])
    valid = bool(run["plan_hash"] and run["plan_hash"] == hashlib.sha256(canon(run["plan_json"]).encode()).hexdigest())
    gold = load_json(ROOT / "fixtures/eval/gold/PCC-DEMO-5.gold.json")
    rec = load_json(ROOT / "fixtures/replay/PCC-DEMO-5.json")["responses"]["plan_story"]
    quality = B2.score_plan(run["planning_json"], gold, agent._oracles("PCC-DEMO-5"), rec) if valid else None
    results = agent.store.results(a["run_id"])
    evidence = evidence_status(out / "data/runs" / a["run_id"], results)
    executed_tools = [r["tool"] for r in agent.store.q("SELECT tool FROM audit WHERE run_id=? AND decision='allow' AND outcome='ok'", (a["run_id"],))]
    detection = B2.detection(run["planning_json"], results, gold) if run["status"] == "completed" and valid else None
    audit = verify_data_dir(out / "data")
    calls = agent.store.run_usage(a["run_id"])
    performance = [json.loads(r["detail"]) for r in agent.store.q("SELECT detail FROM telemetry WHERE run_id=? AND kind='model_performance'", (a["run_id"],))]
    kinds = [r["kind"] for r in agent.store.q("SELECT kind FROM telemetry WHERE run_id=?", (a["run_id"],))]
    result = {"verdict": "SMOKE/NOT_EVALUABLE" if valid else "NOT_EVALUABLE", "independent_quality_certification": False,
              "stage": run["status"], "run_id": a["run_id"], "phase": a["phase"], "model": run["model_id"],
              "model_version": run.get("model_version"), "metadata": a["metadata"], "requester": a["requester"],
              "authenticated_human_approval": reviewed, "approval": approval, "plan_hash": run["plan_hash"],
              "planning": run["planning_json"], "schema_catalog_valid": valid, "quality": quality,
              "usage": calls, "performance": performance, "planning_latency_s": a["latency_s"],
              "telemetry_file": a.get("telemetry_file"),
              "format_repairs": kinds.count("format_error"), "policy_violations": kinds.count("policy_violation"),
              "execution_summary": run["summary_json"], "detection": detection, "results": results,
              "evidence": evidence, "selected_test_types": {t: sum(r["type"] == t for r in results) for t in ("api", "browser")},
              "observed_api_io": "http.request" in executed_tools, "observed_browser_navigation": "browser.navigate" in executed_tools,
              "audit_ok": audit["ok"], "error": run.get("error"),
              "real_reasoning_evidence": bool(valid and not quality["contaminated"] and run.get("model_version") == a["metadata"]["inference"].get("digest") and run["model_label"].startswith("LIVE MODEL (")),
              "report": str(out / "data/runs" / a["run_id"] / "report.html") if results else None}
    result["review_file"] = str(out / "review_plan.json")
    write(out / "b3_summary.json", result)
    return result


def verified_smoke(cfg, out, phase):
    r = summarize(cfg, out)
    if (r["phase"] != phase or r["stage"] != "completed" or not r["authenticated_human_approval"]
            or not r["audit_ok"] or not r["real_reasoning_evidence"] or not r["results"]
            or not r["evidence"]["ok"] or not r["observed_api_io"] or not r["observed_browser_navigation"]):
        raise ValueError("benchmark needs completed, distinct-human-approved real smoke runs in both phases")
    q, d = r["quality"], r["detection"]
    if q["ac_precision"] < .9 or q["ac_recall"] < .95 or q["required_coverage"] < .9 or q["gap_recall"] < 1 or q["skill_action_correct_rate"] < .95:
        raise ValueError("supervised smoke coverage/quality did not meet proposed B2 thresholds")
    if r["execution_summary"]["error"] or r["execution_summary"]["blocked"] or r["policy_violations"]:
        raise ValueError("supervised smoke has execution errors or blocked tests")
    if (phase == "defect_off" and r["execution_summary"]["failed"]) or (phase == "defect_on" and (set(d["detected"]) != set(d["must_fail"]) or d["false_alarms"])):
        raise ValueError("supervised smoke has false alarms or missed seeded defects")
    if identity(cfg)["settings_sha256"] != r["metadata"]["generation"]["settings_sha256"]:
        raise ValueError("model generation configuration changed; repeat supervised smoke")
    if identity(cfg)["configuration_sha256"] != r["metadata"]["generation"]["configuration_sha256"] or source_hashes() != r["metadata"]["source_sha256"]:
        raise ValueError("configuration or planning/runtime sources changed; repeat supervised smoke")
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("diagnose", "plan", "summarize", "benchmark"):
        s = sub.add_parser(name); s.add_argument("--out", type=Path, required=True)
        if name in ("plan", "benchmark"):
            s.add_argument("--users-file", type=Path, required=True)
        if name == "plan":
            s.add_argument("--phase", choices=("defect_on", "defect_off"), required=True)
        if name == "benchmark":
            s.add_argument("--smoke-on", type=Path, required=True); s.add_argument("--smoke-off", type=Path, required=True)
            s.add_argument("--grant"); s.add_argument("--sealed-gold-dir", type=Path)
    a = ap.parse_args(argv)
    try:
        cfg = configuration()
        if a.command == "diagnose":
            _, result = diagnose(cfg, a.out)
            code = 0 if result["inference"]["ready"] else 3
        elif a.command == "plan":
            code, result = plan(cfg, a.out, a.users_file, a.phase)
        elif a.command == "summarize":
            result = summarize(cfg, a.out); code = 3
        else:
            requester(cfg, a.users_file)
            on, off = verified_smoke(cfg, a.smoke_on, "defect_on"), verified_smoke(cfg, a.smoke_off, "defect_off")
            _, result = diagnose(cfg, a.out)
            current = result["inference"]
            if not current["ready"] or any(r["metadata"]["inference"]["digest"] != current["digest"] or r["metadata"]["inference"]["runtime_version"] != current["runtime_version"] for r in (on, off)):
                raise ValueError("installed model/runtime changed/unavailable; repeat supervised smoke before benchmarking")
            effective = copy.deepcopy(cfg); effective["providers"]["ollama"]["version"] = current["digest"]
            config_path = a.out / "agent-b3-effective.json"; write(config_path, effective)
            cmd = [sys.executable, str(ROOT / "scripts/eval_b2.py"), "--provider", "ollama", "--confirm-live-approved",
                   "--requester-users-file", str(a.users_file), "--out", str(a.out)]
            if a.grant: cmd += ["--grant", a.grant]
            if a.sealed_gold_dir: cmd += ["--sealed-gold-dir", str(a.sealed_gold_dir)]
            with sampling(a.out) as telemetry:
                code = subprocess.run(cmd, cwd=ROOT, check=False, env=dict(os.environ, QA_AGENT_CONFIG=str(config_path.resolve()))).returncode
            result = {"exit_code": code, "metadata": result, "telemetry_file": telemetry["file"],
                      "report": str(a.out / "eval_report.json"), "independent_quality_certification": False}
            write(a.out / "b3_benchmark.json", result)
        # Keep benchmark metrics out of the first reviewer handoff. Reviewers use
        # review_plan.json / authenticated UI before inspecting scored summaries.
        printed = {k: result.get(k) for k in ("verdict", "stage", "run_id", "phase", "requester", "plan_hash", "review_file", "error", "reason")} if a.command == "plan" else result
        print(json.dumps(printed, indent=2)); return code
    except (ValueError, OSError, KeyError, ProviderNotApproved, PolicyDenied) as e:
        result = {"verdict": "NOT_EVALUABLE", "reason": redact(str(e)), "real_reasoning_evidence": False}
        write(a.out / "b3_error.json", result); print(json.dumps(result, indent=2)); return 3


if __name__ == "__main__":
    raise SystemExit(main())
