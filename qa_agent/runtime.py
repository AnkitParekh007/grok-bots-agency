"""Generic QA agent runtime: story -> model planning (understanding, ACs, behaviors, tests, gaps) -> validation ->
human approval of the exact plan hash -> execution (single active execution) -> compare -> report.
Domain specifics live in skills + oracle manifests, not here."""
import json, re, threading, uuid
from pathlib import Path
from . import ROOT
from . import planning as P
from .environments import RegistryEnvironmentProvider
from .executor import PlaywrightExecutor
from .gateway import ToolGateway, PolicyDenied
from .providers.mock_jira import MockJiraProvider
from .providers.models import (build_model_provider, ProviderNotApproved, DataClassDenied, ModelCallError, UsageMissing)
from .report import write_report
from .skills import LocalSkillRegistry
from .store import Store, FileArtifactStore, SqliteApprovalProvider
from datetime import datetime, timezone
from .util import canon, load_json, sha256, redact, now

KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-[A-Z0-9]+(?:-[0-9]+)?)\b")
TERMINAL = {"completed", "failed", "rejected", "blocked", "interrupted", "cancelled"}


class PlanInvalid(Exception):
    pass


from .providers.models import BudgetExceeded  # re-exported for callers/tests


class _BudgetLedger:
    """Per-attempt reservation ledger handed to providers that make billable requests."""
    def __init__(self, agent, run_id, provider, model, task, repair_attempt):
        self.a, self.run_id, self.provider, self.model, self.task, self.attempt = agent, run_id, provider, model, task, repair_attempt

    def reserve(self, http_attempt, est):
        uid, reason = self.a.store.reserve_spend(self.run_id, self.provider, self.model, self.task, self.attempt, http_attempt,
                                                 est, self.a.budget["per_run_usd"], self.a.budget["daily_usd"])
        if reason:
            self.a.store.telemetry(self.run_id, "budget_denied", self.provider, reason)
            self.a.gateway.record(self.run_id, "model.budget_gate", {"estimate_usd": est, "http_attempt": http_attempt}, "deny", reason)
            raise BudgetExceeded(reason)
        return uid

    def settle(self, uid, usage, cost, priced, outcome):
        self.a.store.settle_spend(uid, usage, cost, priced, outcome)


class ConflictError(ValueError):
    pass


class QAAgent:
    def __init__(self, data_dir: Path, config_dir: Path = ROOT / "config", agent_cfg: dict | None = None):
        self.cfg = agent_cfg or load_json(Path(config_dir) / "agent.json")
        self.policy = load_json(Path(config_dir) / "policy.json")
        self.store = Store(data_dir)
        self.artifacts = FileArtifactStore(Path(data_dir) / "runs")
        self.approvals = SqliteApprovalProvider(self.store)
        self.envs = RegistryEnvironmentProvider(load_json(Path(config_dir) / "environments.json"))
        self.gateway = ToolGateway(self.policy, self.store, self.approvals, self.envs, self.artifacts, Path(data_dir) / "audit.jsonl")
        self.tracker = MockJiraProvider(ROOT / self.cfg["issue_tracker"]["fixtures_dir"])
        self.skills = LocalSkillRegistry()
        self.model = build_model_provider(self.cfg, ROOT, gateway=self.gateway)  # fail-closed if not approved/configured
        self.budget = {"per_run_usd": 10.0, "daily_usd": 150.0, **self.cfg.get("budget", {})}
        self.max_repairs = int(self.cfg.get("repair", {}).get("max_format_repairs", 2))
        self.exec_cfg = self.cfg.get("execution", {})
        self.exec_lock = threading.Lock()
        self.plan_ttl_min = float(self.cfg.get("plan", {}).get("ttl_minutes", 60))
        self.threads = []

    # ---------- public API ----------
    def start_run(self, request: str, environment: str | None = None, story_key: str | None = None, wait=False, requested_by=None):
        env_name = environment or self.cfg["default_environment"]
        if not story_key:
            m = KEY_RE.search(request or "")
            story_key = m.group(1) if m else ("PCC-DEMO-1" if "synthetic supplier import" in (request or "").lower() else None)
        run_id = "run-" + uuid.uuid4().hex[:10]
        self.store.create_run(run_id, request, env_name, self.model.name, self.model.label)
        if requested_by:
            self.store.update_run(run_id, requested_by=requested_by)
        self.store.event(run_id, "start", f"Request received: {request!r}")
        return self._spawn(run_id, self._plan, run_id, story_key, env_name, wait=wait)

    def approve(self, run_id, approver, decision, comment="", wait=False, identity=None, auth_mode="demo",
                plan_hash=None, approver_label=None):
        run = self.store.get_run(run_id)
        if not run or run["status"] != "awaiting_approval":
            raise ConflictError(f"run not awaiting approval (status={run and run['status']})")
        if self._plan_expired(run):
            self.gateway.record(run_id, "plan.approve", {"plan_hash": run["plan_hash"]}, "deny", "plan expired; re-plan required")
            raise ConflictError(f"plan expired (older than {self.plan_ttl_min} min); start a new run to re-plan")
        if plan_hash is not None and plan_hash != run["plan_hash"]:
            self.gateway.record(run_id, "plan.approve", {"plan_hash": plan_hash}, "deny", "stale plan hash: approval refers to a different plan")
            raise ConflictError("stale plan: the plan changed since it was displayed; reload and review again")
        if decision == "approved" and not self.exec_lock.acquire(blocking=False):
            raise ConflictError("another execution is active against the shared synthetic SUT; try again when it finishes")
        try:
            self.approvals.record(run_id, run["plan_hash"], approver, decision, comment, identity=identity,
                                  auth_mode=auth_mode, approver_label=approver_label)
            who = f"{approver} ({'authenticated' if identity else 'UNAUTHENTICATED demo label'})"
            self.store.event(run_id, "approval", f"Plan {decision} by {who} (plan sha256 {run['plan_hash'][:12]})")
            if decision != "approved":
                self.store.update_run(run_id, status="rejected")
                return run_id
            self.store.update_run(run_id, status="executing")
        except Exception:
            if decision == "approved":
                self.exec_lock.release()
            raise
        return self._spawn(run_id, self._execute_locked, run_id, wait=wait)

    def _plan_expired(self, run):
        ts = run.get("plan_created_at")
        if not ts:
            return False   # runs planned before B1.1 have no timestamp
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 60
        return age > self.plan_ttl_min

    def execute(self, run_id, wait=True):
        """Direct execution entry (used to prove fail-closed); still gated by approval + gateway."""
        if not self.exec_lock.acquire(blocking=False):
            raise ConflictError("another execution is active")
        return self._spawn(run_id, self._execute_locked, run_id, wait=wait)

    def cancel(self, run_id, by="unknown"):
        run = self.store.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] in TERMINAL:
            raise ConflictError(f"run already {run['status']}")
        self.store.update_run(run_id, cancel_requested=1)
        self.store.event(run_id, "cancel", f"Cancellation requested by {by}")
        if run["status"] in ("awaiting_approval", "planning"):
            self.store.update_run(run_id, status="cancelled")
        return run_id

    def is_cancelled(self, run_id):
        r = self.store.q("SELECT cancel_requested FROM runs WHERE id=?", (run_id,))
        return bool(r and r[0]["cancel_requested"])

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
            msg = redact(f"{type(e).__name__}: {e}")
            self.store.event(run_id, "error", msg)
            self.store.update_run(run_id, status="failed", error=msg)

    def _tool(self, run_id, tool, args, fn, ctx=None):
        return self.gateway.invoke(run_id, tool, args, fn, ctx)

    def _oracles(self, story_key):
        p = ROOT / self.cfg["oracles_dir"] / f"{story_key}.expected.json"
        if not p.exists():
            raise PlanInvalid(f"no expected-results manifest for {story_key}; cannot test without an oracle")
        return load_json(p)

    def fixture_files(self):
        d = ROOT / self.cfg["fixture_data_dir"]
        return sorted(p.name for p in d.glob("*.csv"))

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
        labels = story["fields"].get("labels") or []
        data_class = "synthetic" if ("synthetic" in labels and env.get("data_class") == "synthetic") else "unclassified"
        ev("data_class", f"Data classification for model input: {data_class}")
        oracles = self._oracles(story_key)
        catalog = P.build_catalog(self.skills, oracles, self.fixture_files())
        mctx = {"story_key": story_key, "story": story, "data_class": data_class, "catalog": catalog}
        planning = self._model_plan(run_id, mctx, desc, oracles)

        gaps = planning["coverage_gaps"]
        ac = {"acceptance_criteria": planning["acceptance_criteria"], "gaps": [g["reason"] for g in gaps]}
        plan = {"test_cases": planning["test_cases"]}
        plan_hash = sha256(canon(plan))
        self.store.update_run(run_id, ac_json=ac, planning_json=planning, plan_json=plan, plan_hash=plan_hash,
                              catalog_version=catalog["catalog_version"], plan_created_at=now(), status="awaiting_approval")
        cats = {c: sum(t["category"] == c for t in plan["test_cases"]) for c in P.CATEGORIES}
        ev("ac", f"[{self.model.label}] {len(ac['acceptance_criteria'])} acceptance criteria, each traced to verbatim story text")
        ev("plan", f"[{self.model.label}] Test plan: {len(plan['test_cases'])} cases {cats}, {len(gaps)} coverage gap(s) (sha256 {plan_hash[:12]})")
        ev("approval", "Waiting for human approval. Execution is blocked until the exact plan is approved.")

    def _model_plan(self, run_id, mctx, desc, oracles):
        feedback, attempt = None, 0
        pname = self.model.name
        while True:
            billable = getattr(self.model, "per_attempt_budget", False)
            if billable:
                ledger = _BudgetLedger(self, run_id, pname, getattr(self.model, "model", pname), "plan_story", attempt)
                call = lambda: self.model.generate("plan_story", mctx, P.PLAN_STORY_SCHEMA, feedback, run_id=run_id, budget=ledger)
            else:
                est = self.model.estimate_max_cost(len(json.dumps(P.build_messages("plan_story", mctx, feedback))))
                self._budget_check(run_id, est)
                call = lambda: self.model.generate("plan_story", mctx, P.PLAN_STORY_SCHEMA, feedback, run_id=run_id)
            try:
                resp = self._tool(run_id, "model.complete",
                                  {"task": "plan_story", "story": mctx["story_key"], "provider": pname, "repair_attempt": attempt}, call)
            except DataClassDenied as e:
                self.store.telemetry(run_id, "data_class_denied", pname, str(e))
                self.gateway.record(run_id, "model.data_class_gate", {"data_class": mctx["data_class"]}, "deny", str(e))
                raise
            except UsageMissing as e:
                # never invent usage: token counts stay NULL; the conservative estimate stays charged against the budget
                if not billable:
                    self.store.add_usage(run_id, pname, getattr(self.model, "model", pname), "plan_story", attempt, None, est, False, "usage_missing")
                self.store.telemetry(run_id, "usage_missing", pname, str(e))
                raise
            except ModelCallError as e:
                self.store.telemetry(run_id, f"model_call_failed:{e.kind}", pname, json.dumps(e.as_dict()))
                raise   # no automatic fallback to another provider
            except (ProviderNotApproved, PolicyDenied) as e:
                self.store.telemetry(run_id, "model_call_failed", pname, f"{type(e).__name__}: {e}")
                raise   # no automatic fallback to another provider
            if not billable:   # billable providers already settled every HTTP attempt in the ledger
                self.store.add_usage(run_id, resp.provider, resp.model, "plan_story", attempt, resp.usage, resp.cost_usd, resp.priced, resp.finish_reason)
            self.store.telemetry(run_id, "model_call_ok", pname, f"attempts={resp.attempts} tokens={resp.usage.get('total_tokens', 0)} cost={resp.cost_usd}")
            try:
                obj = P.parse_output(resp.text, resp.finish_reason)
                return P.validate_plan(obj, desc, self.skills, oracles, self.fixture_files())
            except P.PolicyViolation as e:
                self.store.telemetry(run_id, "policy_violation", pname, str(e))
                self.gateway.record(run_id, "model.output_validation", {"attempt": attempt, "output_sha256": sha256(resp.text or "")},
                                    "deny", f"policy violation (not repairable): {e}")
                self.store.event(run_id, "rejected", f"Model output rejected (policy, no repair): {e}")
                raise PlanInvalid(str(e))
            except P.FormatError as e:
                self.store.telemetry(run_id, "format_error", pname, str(e))
                self.gateway.record(run_id, "model.output_validation", {"attempt": attempt, "output_sha256": sha256(resp.text or "")},
                                    "deny", f"format error: {e}")
                if attempt >= self.max_repairs:
                    raise PlanInvalid(f"model output failed format validation after {attempt + 1} attempt(s): {e}")
                attempt += 1
                feedback = str(e)
                self.store.event(run_id, "repair", f"Format error, requesting bounded repair {attempt}/{self.max_repairs}: {e}")

    def _budget_check(self, run_id, est):
        run_spent, day_spent = self.store.run_spend(run_id), self.store.day_spend()
        reason = None
        if run_spent + est > self.budget["per_run_usd"]:
            reason = f"per-run cap ${self.budget['per_run_usd']} would be exceeded (spent ${run_spent:.4f} + est ${est:.4f})"
        elif day_spent + est > self.budget["daily_usd"]:
            reason = f"daily cap ${self.budget['daily_usd']} would be exceeded (spent ${day_spent:.4f} + est ${est:.4f})"
        if reason:
            self.store.telemetry(run_id, "budget_denied", self.model.name, reason)
            self.gateway.record(run_id, "model.budget_gate", {"estimate_usd": est}, "deny", reason)
            raise BudgetExceeded(reason)

    def _execute_locked(self, run_id):
        try:
            self._execute(run_id)
        finally:
            if self.exec_lock.locked():
                self.exec_lock.release()

    def _execute(self, run_id):
        run = self.store.get_run(run_id)
        ev = lambda s, m: self.store.event(run_id, s, m)
        if not self.approvals.is_approved(run_id, run["plan_hash"]) or run["plan_hash"] != sha256(canon(run["plan_json"] or {})):
            self.store.update_run(run_id, status="blocked", error="execution refused: no approval for this exact plan")
            self.gateway.record(run_id, "plan.execute", {"plan_hash": run["plan_hash"]}, "deny", "no approval for this exact plan (or plan modified after approval)")
            ev("blocked", "Execution refused: no human approval recorded for this exact plan (fail-closed)")
            return
        cur = P.catalog_version(self.skills)
        if run.get("catalog_version") and run["catalog_version"] != cur:
            self.store.update_run(run_id, status="blocked", error=f"action catalog changed since planning ({run['catalog_version']} -> {cur}); re-plan")
            self.gateway.record(run_id, "plan.execute", {"catalog_version": run["catalog_version"], "current": cur}, "deny", "stale action catalog")
            ev("blocked", "Execution refused: the skill/action catalog changed after the plan was approved")
            return
        self.store.update_run(run_id, status="executing")
        env = self._tool(run_id, "env.resolve", {"name": run["environment"]}, lambda: self.envs.resolve(run["environment"]))
        oracles = self._oracles(run["story_key"])["oracles"]
        ex = PlaywrightExecutor(self.gateway, self.artifacts, self.skills, oracles, ROOT / self.cfg["fixture_data_dir"], self.store,
                                timeouts=self.exec_cfg, is_cancelled=self.is_cancelled)
        ev("execute", f"Launching headless Chromium against {env['base_url']}")
        results = ex.run(run_id, run["plan_json"], env, ev, plan_hash=run["plan_hash"])
        counts = {s: sum(r["status"] == s for r in results) for s in ("passed", "failed", "error", "blocked")}
        summary = {"total": len(results), **counts}
        self.store.update_run(run_id, summary_json=summary)
        cancelled = self.is_cancelled(run_id)
        rep = self._tool(run_id, "report.generate", {"run_id": run_id}, lambda: write_report(self, run_id))
        ev("report", f"Report written (sha256 {rep['sha256'][:12]})")
        not_run = sum(r["status"] == "not_run" for r in results)
        self.store.update_run(run_id, status="cancelled" if cancelled else "completed")
        ev("done", f"{'Cancelled' if cancelled else 'Completed'}: {summary['passed']} passed, {summary['failed']} failed, "
                   f"{summary['error']} error, {summary['blocked']} blocked, {not_run} not run")
