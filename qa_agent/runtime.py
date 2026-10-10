"""Generic QA agent runtime: story read -> AC extraction -> plan -> human approval -> execute -> compare -> report.
Domain specifics live in skills + oracle manifests, not here."""
import json, re, threading, uuid
from pathlib import Path
import jsonschema
from . import ROOT
from .environments import RegistryEnvironmentProvider, EnvironmentRefused
from .executor import PlaywrightExecutor
from .gateway import ToolGateway, PolicyDenied
from .providers.mock_jira import MockJiraProvider
from .providers.models import build_model_provider
from .report import write_report
from .schemas import AC_SCHEMA, PLAN_SCHEMA
from .skills import LocalSkillRegistry
from .store import Store, FileArtifactStore, SqliteApprovalProvider
from .util import canon, load_json, sha256

KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-[A-Z0-9]+(?:-[0-9]+)?)\b")


class PlanInvalid(Exception):
    pass


class QAAgent:
    def __init__(self, data_dir: Path, config_dir: Path = ROOT / "config"):
        self.cfg = load_json(config_dir / "agent.json")
        self.policy = load_json(config_dir / "policy.json")
        self.store = Store(data_dir)
        self.artifacts = FileArtifactStore(Path(data_dir) / "runs")
        self.approvals = SqliteApprovalProvider(self.store)
        self.envs = RegistryEnvironmentProvider(load_json(config_dir / "environments.json"))
        self.gateway = ToolGateway(self.policy, self.store, self.approvals, self.envs, self.artifacts, Path(data_dir) / "audit.jsonl")
        self.tracker = MockJiraProvider(ROOT / self.cfg["issue_tracker"]["fixtures_dir"])
        self.skills = LocalSkillRegistry()
        self.model = build_model_provider(self.cfg, ROOT)  # raises (fail-closed) if provider not approved
        self.threads = []

    # ---------- public API ----------
    def start_run(self, request: str, environment: str | None = None, story_key: str | None = None, wait=False):
        env_name = environment or self.cfg["default_environment"]
        if not story_key:
            m = KEY_RE.search(request or "")
            story_key = m.group(1) if m else ("PCC-DEMO-1" if "synthetic supplier import" in (request or "").lower() else None)
        run_id = "run-" + uuid.uuid4().hex[:10]
        self.store.create_run(run_id, request, env_name, self.model.name, self.model.label)
        self.store.event(run_id, "start", f"Request received: {request!r}")
        return self._spawn(run_id, self._plan, run_id, story_key, env_name, wait=wait)

    def approve(self, run_id, approver, decision, comment="", wait=False):
        run = self.store.get_run(run_id)
        if not run or run["status"] != "awaiting_approval":
            raise ValueError(f"run not awaiting approval (status={run and run['status']})")
        self.approvals.record(run_id, run["plan_hash"], approver, decision, comment)
        self.store.event(run_id, "approval", f"Plan {decision} by {approver} (plan sha256 {run['plan_hash'][:12]})")
        if decision != "approved":
            self.store.update_run(run_id, status="rejected")
            return run_id
        self.store.update_run(run_id, status="executing")
        return self._spawn(run_id, self._execute, run_id, wait=wait)

    def execute(self, run_id, wait=True):
        """Direct execution entry (used to prove fail-closed); still gated by the gateway."""
        return self._spawn(run_id, self._execute, run_id, wait=wait)

    # ---------- internals ----------
    def _spawn(self, run_id, fn, *a, wait=False):
        th = threading.Thread(target=self._guard, args=(run_id, fn, *a), daemon=True)
        self.threads.append(th)
        th.start()
        if wait:
            th.join()
        return run_id

    def _guard(self, run_id, fn, *a):
        try:
            fn(*a)
        except Exception as e:
            self.store.event(run_id, "error", f"{type(e).__name__}: {e}")
            self.store.update_run(run_id, status="failed", error=f"{type(e).__name__}: {e}")

    def _tool(self, run_id, tool, args, fn, ctx=None):
        return self.gateway.invoke(run_id, tool, args, fn, ctx)

    def _plan(self, run_id, story_key, env_name):
        ev = lambda s, m: self.store.event(run_id, s, m)
        if not story_key:
            raise ValueError("no story key found in request")
        env = self._tool(run_id, "env.resolve", {"name": env_name}, lambda: self.envs.resolve(env_name))
        ev("environment", f"Environment '{env_name}' resolved -> {env['base_url']} ({env['status']})")
        story = self._tool(run_id, "tracker.get_issue", {"key": story_key}, lambda: self.tracker.get_issue(story_key))
        self.store.update_run(run_id, story_key=story_key, story_json=story)
        ev("story", f"Read {story_key}: {story['fields']['summary']}")
        desc = story["fields"]["description"]
        mctx = {"story_key": story_key, "story": story, "data_class": env.get("data_class", "synthetic")}

        ac = self._tool(run_id, "model.complete", {"task": "extract_acceptance_criteria", "story": story_key, "provider": self.model.name},
                        lambda: self.model.complete("extract_acceptance_criteria", mctx))
        self._validate(ac, AC_SCHEMA, "AC extraction")
        numbered = set(re.findall(r"^\s*(\d+)\.", desc, re.M))
        for a in ac["acceptance_criteria"]:
            if a["id"][2:] not in numbered:
                raise PlanInvalid(f"{a['id']} does not trace to a numbered AC in the story")
            if a["source_quote"] not in desc:
                raise PlanInvalid(f"{a['id']} source_quote not found verbatim in story")
        self.store.update_run(run_id, ac_json=ac)
        ev("ac", f"[{self.model.label}] Extracted {len(ac['acceptance_criteria'])} acceptance criteria, each traced to story text")

        oracles = self._oracles(story_key)
        mctx["acceptance_criteria"] = ac["acceptance_criteria"]
        mctx["skills"] = self.skills.list()
        plan = self._tool(run_id, "model.complete", {"task": "generate_test_plan", "story": story_key, "provider": self.model.name},
                          lambda: self.model.complete("generate_test_plan", mctx))
        self._validate(plan, PLAN_SCHEMA, "test plan")
        ac_ids = {a["id"] for a in ac["acceptance_criteria"]}
        for tc in plan["test_cases"]:
            skill = self.skills.get(tc["skill"])
            if tc["action"] not in skill.actions:
                raise PlanInvalid(f"{tc['id']}: action {tc['action']} not provided by skill {tc['skill']}")
            if skill.actions[tc["action"]][0] != tc["type"]:
                raise PlanInvalid(f"{tc['id']}: type mismatch")
            if not set(tc["ac_refs"]) <= ac_ids:
                raise PlanInvalid(f"{tc['id']}: unknown AC refs {tc['ac_refs']}")
            if tc["oracle_ref"] not in oracles["oracles"]:
                raise PlanInvalid(f"{tc['id']}: oracle {tc['oracle_ref']} not in approved manifest")
        uncovered = ac_ids - {r for tc in plan["test_cases"] for r in tc["ac_refs"]}
        plan_hash = sha256(canon(plan))
        self.store.update_run(run_id, plan_json=plan, plan_hash=plan_hash, status="awaiting_approval")
        ev("plan", f"[{self.model.label}] Test plan with {len(plan['test_cases'])} cases ready (sha256 {plan_hash[:12]})"
           + (f"; UNCOVERED: {sorted(uncovered)}" if uncovered else "; all ACs covered"))
        ev("approval", "Waiting for human approval. Execution is blocked until the exact plan is approved.")

    def _validate(self, obj, schema, what):
        try:
            jsonschema.validate(obj, schema)
        except jsonschema.ValidationError as e:
            raise PlanInvalid(f"model output for {what} failed schema validation: {e.message}")

    def _oracles(self, story_key):
        p = ROOT / self.cfg["oracles_dir"] / f"{story_key}.expected.json"
        if not p.exists():
            raise PlanInvalid(f"no expected-results manifest for {story_key}; cannot test without an oracle")
        return load_json(p)

    def _execute(self, run_id):
        run = self.store.get_run(run_id)
        ev = lambda s, m: self.store.event(run_id, s, m)
        if not self.approvals.is_approved(run_id, run["plan_hash"]):
            self.store.update_run(run_id, status="blocked", error="execution refused: no approval for this plan")
            ev("blocked", "Execution refused: no human approval recorded for this exact plan (fail-closed)")
            return
        self.store.update_run(run_id, status="executing")
        env = self._tool(run_id, "env.resolve", {"name": run["environment"]}, lambda: self.envs.resolve(run["environment"]))
        oracles = self._oracles(run["story_key"])["oracles"]
        ex = PlaywrightExecutor(self.gateway, self.artifacts, self.skills, oracles, ROOT / self.cfg["fixture_data_dir"], self.store)
        ev("execute", f"Launching headless Chromium against {env['base_url']}")
        results = ex.run(run_id, run["plan_json"], env, ev, plan_hash=run["plan_hash"])
        counts = {s: sum(r["status"] == s for r in results) for s in ("passed", "failed", "error", "blocked")}
        summary = {"total": len(results), **counts}
        self.store.update_run(run_id, summary_json=summary)
        rep = self._tool(run_id, "report.generate", {"run_id": run_id},
                         lambda: write_report(self, run_id))
        ev("report", f"Report written (sha256 {rep['sha256'][:12]})")
        self.store.update_run(run_id, status="completed")
        ev("done", f"Completed: {summary['passed']} passed, {summary['failed']} failed, {summary['error']} error, {summary['blocked']} blocked")
