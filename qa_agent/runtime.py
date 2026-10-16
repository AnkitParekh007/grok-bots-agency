"""Generic QA agent runtime: story -> model planning (understanding, ACs, behaviors, tests, gaps) -> validation ->
human approval of the exact plan hash -> execution (single active execution) -> compare -> report.
Domain specifics live in skills + oracle manifests, not here."""
import json, math, re, threading, uuid
from pathlib import Path
from . import ROOT
from . import planning as P
from . import execution_package as EP
from .environments import RegistryEnvironmentProvider
from .executor import PlaywrightExecutor
from .gateway import ToolGateway, PolicyDenied
from .providers.mock_jira import MockJiraProvider
from .integrations.registry import build_tracker, build_git
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


from .providers.models import BudgetExceeded, ProviderConfigError  # re-exported for callers/tests


class _BudgetLedger:
    """Per-attempt reservation ledger handed to providers that make billable requests."""
    def __init__(self, agent, run_id, provider, model, task, repair_attempt):
        self.a, self.run_id, self.provider, self.model, self.task, self.attempt = agent, run_id, provider, model, task, repair_attempt

    def reserve(self, http_attempt, est):
        uid, reason = self.a.store.reserve_spend(self.run_id, self.provider, self.model, self.task, self.attempt, http_attempt,
                                                 est, self.a.budget["per_run_usd"], self.a.budget["daily_usd"],
                                                 self.a.budget["total_usd"], self.a.budget["scope"])
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
    def __init__(self, data_dir: Path, config_dir: Path = ROOT / "config", agent_cfg: dict | None = None, review_only=False):
        self.cfg = agent_cfg or load_json(Path(config_dir) / "agent.json")
        self.review_only = review_only
        self.policy = load_json(Path(config_dir) / "policy.json")
        self.store = Store(data_dir)
        self.artifacts = FileArtifactStore(Path(data_dir) / "runs")
        self.approvals = SqliteApprovalProvider(self.store)
        self.envs = RegistryEnvironmentProvider(load_json(Path(config_dir) / "environments.json"))
        self.gateway = ToolGateway(self.policy, self.store, self.approvals, self.envs, self.artifacts, Path(data_dir) / "audit.jsonl")
        self.tracker = build_tracker(self.cfg, gateway=self.gateway)   # mock_jira by default; real adapters fail closed
        self.git = build_git(self.cfg, gateway=self.gateway)           # synthetic_git by default
        self.projects = {k: v for k, v in (self.cfg.get("projects") or {}).items() if not k.startswith("_")}
        from .authorization import AccessPolicy
        self.shared = self.cfg.get("deployment", {}).get("profile") == "shared"
        self.access = AccessPolicy(self.projects, self.cfg.get("authorization"), shared=self.shared)
        from .integrations.broker import ConnectionBroker
        self.connections = ConnectionBroker(self.cfg.get("connections"), self.store, self.gateway, self.projects)
        self.skills = LocalSkillRegistry()
        self.model = build_model_provider(self.cfg, ROOT, gateway=self.gateway,
                     selected="replay" if review_only else (self.cfg["model"]["provider"] if agent_cfg is not None else None))
        self.budget = {"per_run_usd": 1.0, "daily_usd": 15.0, **self.cfg.get("budget", {})}
        if getattr(self.model, "per_attempt_budget", False):
            # billable provider: every cap must be explicitly configured, positive and numeric (fail closed)
            for k in ("per_run_usd", "daily_usd", "total_usd"):
                v = self.budget.get(k)
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
                    raise ProviderConfigError(f"budget.{k} must be a positive number for a billable provider (got {v!r})")
            if not isinstance(self.budget.get("scope"), str) or not self.budget["scope"].strip():
                raise ProviderConfigError("budget.scope (persistent total-cap scope id) is required for a billable provider")
        self.budget.setdefault("total_usd", None); self.budget.setdefault("scope", None)
        self.max_repairs = int(self.cfg.get("repair", {}).get("max_format_repairs", 2))
        self.exec_cfg = self.cfg.get("execution", {})
        self.exec_lock = threading.Lock()
        self.plan_ttl_min = float(self.cfg.get("plan", {}).get("ttl_minutes", 60))
        self.threads = []
        self._run_models = {}
        from .providers.registry import ModelRegistry
        self.models = ModelRegistry(self)

    # ---------- public API ----------
    def tracker_for(self, project=None):
        return self.connections.adapter(project, "tracker") if self.projects.get(project, {}).get("tracker_connection") else self.tracker

    def git_for(self, project=None):
        return self.connections.adapter(project, "git") if self.projects.get(project, {}).get("git_connection") else self.git

    def select(self, project=None, story_key=None, repository=None, environment=None):
        """Validate a workspace selection (project -> story / repository / environment). Raises ValueError."""
        if not project:
            if repository:
                raise ValueError("repository selection requires a project")
            return None
        pc = self.projects.get(project)
        if not pc:
            raise ValueError(f"project {project!r} is not configured")
        if story_key and not story_key.startswith(pc["jira_project"] + "-"):
            raise ValueError(f"story {story_key} is not in project {pc['jira_project']}")
        if repository and repository not in pc.get("repositories", []):
            raise ValueError(f"repository {repository!r} is not linked to project {project}")
        if environment and environment not in pc.get("environments", []):
            raise ValueError(f"environment {environment!r} is not linked to project {project}")
        return pc

    def start_run(self, request: str, environment: str | None = None, story_key: str | None = None, wait=False, requested_by=None,
                  project: str | None = None, repository: str | None = None,
                  model_provider: str | None = None, model_id: str | None = None, tenant_id="local"):
        if self.review_only:
            raise ConflictError("review-only agent cannot plan or execute")
        model = self.models.resolve(model_provider, model_id) if model_provider else self.model
        if model_id and not model_provider:
            raise ProviderNotApproved("model_id requires model_provider")
        if getattr(model, "per_attempt_budget", False):
            for cap in ("per_run_usd", "daily_usd", "total_usd"):
                if not isinstance(self.budget.get(cap), (int, float)) or isinstance(self.budget.get(cap), bool) or not math.isfinite(self.budget[cap]) or self.budget[cap] <= 0:
                    raise ProviderConfigError("paid model requires configured dollar caps")
            if not self.budget.get("scope"): raise ProviderConfigError("paid model requires budget scope")
        env_name = environment or self.cfg["default_environment"]
        if project or repository:
            self.select(project, story_key, repository, environment)
        if not story_key:
            m = KEY_RE.search(request or "")
            story_key = m.group(1) if m else ("PCC-DEMO-1" if "synthetic supplier import" in (request or "").lower() else None)
        run_id = "run-" + uuid.uuid4().hex[:10]
        self._run_models[run_id] = model
        self.store.create_run(run_id, request, env_name, model.name, model.label)
        self.store.update_run(run_id, tenant_id=tenant_id)
        self.store.update_run(run_id, model_id=getattr(model, "model", model.name), model_version=getattr(model, "version", None))
        self.gateway.record(run_id, "model.selected", {"provider": model.name, "model": getattr(model, "model", model.name), "version": getattr(model, "version", None)}, "allow", "model selected for new run")
        if requested_by:
            self.store.update_run(run_id, requested_by=requested_by)
        if project or repository:
            self.store.update_run(run_id, project=project, repository=repository)
        self.store.event(run_id, "start", f"Request received: {request!r}")
        return self._spawn(run_id, self._plan, run_id, story_key, env_name, wait=wait)

    def approve(self, run_id, approver, decision, comment="", wait=False, identity=None, auth_mode="demo",
                plan_hash=None, approver_label=None):
        if self.review_only:
            raise ConflictError("review-only agent cannot dispatch execution")
        run = self.store.get_run(run_id)
        if not run or run["status"] != "awaiting_approval":
            raise ConflictError(f"run not awaiting approval (status={run and run['status']})")
        if self._plan_expired(run):
            self.gateway.record(run_id, "plan.approve", {"plan_hash": run["plan_hash"]}, "deny", "plan expired; re-plan required")
            raise ConflictError(f"plan expired (older than {self.plan_ttl_min} min); start a new run to re-plan")
        if plan_hash is not None and plan_hash != run["plan_hash"]:
            self.gateway.record(run_id, "plan.approve", {"plan_hash": plan_hash}, "deny", "stale plan hash: approval refers to a different plan")
            raise ConflictError("stale plan: the plan changed since it was displayed; reload and review again")
        if decision == "approved":
            self._verify_package(run)
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

    def review_plan(self, run_id, principal, decision, plan_hash, comment=""):
        """Manual review with DEFERRED execution (eval first-plan handoff; CLI). Same rules as the HTTP approval in local
        mode: authenticated approver, no self-approval, exact plan hash, not expired. Does not start execution."""
        run = self.store.get_run(run_id)
        def deny(reason):
            self.gateway.record(run_id, "plan.review", {"user": getattr(principal, "name", None), "plan_hash": plan_hash}, "deny", reason)
            raise ConflictError(reason)
        if principal is None or not principal.authenticated or principal.kind != "human":
            deny("authenticated human approver required")
        from .auth import has_role
        if not has_role(principal, "approver"):
            deny(f"role {principal.role} cannot approve")
        if not run or run["status"] != "awaiting_approval":
            deny(f"run not awaiting approval (status={run and run['status']})")
        if self.access.strict and (run.get("tenant_id") != principal.tenant_id or not self.access.allows(principal, run.get("project"), "approver")):
            deny("approver is outside the run's organization/project scope")
        if run.get("requested_by") and run["requested_by"] == principal.name:
            deny("requester cannot approve their own run")
        if not plan_hash or plan_hash != run["plan_hash"]:
            deny("stale or missing plan hash: the plan reviewed is not the plan stored")
        if self._plan_expired(run):
            deny("plan expired; re-plan required")
        if decision == "approved":
            self._verify_package(run)
        self.approvals.record(run_id, run["plan_hash"], principal.name, decision, comment, identity=principal.name, auth_mode="local")
        self.store.update_run(run_id, status="approved" if decision == "approved" else "rejected")
        self.gateway.record(run_id, "plan.review", {"user": principal.name, "plan_hash": plan_hash, "decision": decision}, "allow",
                            "manual review by authenticated approver (execution deferred)")
        self.store.event(run_id, "approval", f"Plan {decision} by {principal.name} (authenticated, manual review; execution deferred)")
        return {"run_id": run_id, "decision": decision, "plan_hash": run["plan_hash"], "approver": principal.name}

    def execute_approved(self, run_id, wait=True):
        """Execute a run whose exact plan was approved via review_plan()."""
        if self.review_only:
            raise ConflictError("review-only agent cannot dispatch execution")
        run = self.store.get_run(run_id)
        a = self.approvals.latest(run_id)
        if not run or run["status"] != "approved" or not self.approvals.is_approved(run_id, run["plan_hash"]) \
                or not a or not a.get("identity"):
            self.gateway.record(run_id, "plan.execute", {"status": run and run["status"]}, "deny", "no verified approval for this exact plan")
            raise ConflictError("run has no verified approval for its exact plan")
        if not self.exec_lock.acquire(blocking=False):
            raise ConflictError("another execution is active against the shared synthetic SUT")
        self.store.update_run(run_id, status="executing")
        return self._spawn(run_id, self._execute_locked, run_id, wait=wait)

    def approve_unattended(self, run_id, grant_id, wait=False):
        """Synthetic-eval-only unattended approval under a server-side grant. Never exposed over HTTP."""
        from . import eval_authorization as EA
        run = self.store.get_run(run_id)
        def deny(reason):
            self.gateway.record(run_id, "plan.approve_unattended", {"grant_id": grant_id}, "deny", reason)
            self.store.event(run_id, "approval", f"Unattended approval refused: {reason}")
            raise EA.AuthorizationError(reason)
        if not self.cfg.get("unattended_eval_approval", {}).get("enabled") is True:
            deny("unattended eval approval disabled in agent config (unattended_eval_approval.enabled=false)")
        if not run or run["status"] != "awaiting_approval":
            deny(f"run not awaiting approval (status={run and run['status']})")
        try:
            g = EA.load_grant(self.store, grant_id)
            EA.check(g, run, self.envs.resolve(run["environment"]))
        except EA.AuthorizationError as e:
            deny(str(e))
        except Exception as e:
            deny(f"{type(e).__name__}: {e}")
        mc = EA.model_class(run.get("model_label"))
        if (mc == "LIVE MODEL" or g["require_first_manual_approval"]) and not self.store.has_manual_authenticated_approval(mc):
            deny(f"no prior MANUAL approval by an authenticated approver for a {mc!r} plan; the first plan must be human-reviewed")
        ok, used = self.store.claim_grant_use(grant_id, run_id)
        if not ok:
            deny(f"grant {grant_id} exhausted or revoked ({used}/{g['max_uses']} uses)")
        self.gateway.record(run_id, "plan.approve_unattended", {"grant_id": grant_id, "use": used + 1, "max_uses": g["max_uses"],
                            "model_class": mc, "grant_creator": g["created_by"]}, "allow", "within server-side eval grant")
        return self.approve(run_id, f"unattended-eval-grant:{g['created_by']}", "approved",
                            comment=f"unattended synthetic-eval approval under grant {grant_id} ({used + 1}/{g['max_uses']})",
                            wait=wait, identity=g["created_by"], auth_mode="unattended-synthetic-eval",
                            plan_hash=run["plan_hash"], approver_label=grant_id)

    def _plan_expired(self, run):
        ts = run.get("plan_created_at")
        if not ts:
            return False   # runs planned before B1.1 have no timestamp
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 60
        return age > self.plan_ttl_min

    def execute(self, run_id, wait=True):
        """Direct execution entry (used to prove fail-closed); still gated by approval + gateway."""
        if self.review_only:
            raise ConflictError("review-only agent cannot dispatch execution")
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
        finally:
            self._run_models.pop(run_id, None)

    def _tool(self, run_id, tool, args, fn, ctx=None):
        return self.gateway.invoke(run_id, tool, args, fn, ctx)

    def oracle_path(self, story_key):
        root = (ROOT / self.cfg["oracles_dir"]).resolve()
        p = (root / f"{story_key}.expected.json").resolve()
        if not p.is_relative_to(root) or p.parent != root:
            raise PlanInvalid("oracle path outside configured directory")
        return p

    def _oracles(self, story_key):
        p = self.oracle_path(story_key)
        if not p.exists():
            raise PlanInvalid(f"no expected-results manifest for {story_key}; cannot test without an oracle")
        return load_json(p)

    def fixture_files(self):
        d = ROOT / self.cfg["fixture_data_dir"]
        return sorted(p.name for p in d.glob("*.csv"))

    def _plan(self, run_id, story_key, env_name):
        model = self._run_models.get(run_id, self.model)
        ev = lambda s, m: self.store.event(run_id, s, m)
        if not story_key:
            raise ValueError("no story key found in request")
        env = self._tool(run_id, "env.resolve", {"name": env_name}, lambda: self.envs.resolve(env_name))
        ev("environment", f"Environment '{env_name}' resolved -> {env['base_url']} ({env['status']})")
        run0 = self.store.get_run(run_id)
        if run0.get("project"):
            self.select(run0["project"], story_key, run0.get("repository"), env_name)   # selection re-checked server-side
        tracker, git = self.tracker_for(run0.get("project")), self.git_for(run0.get("project"))
        story = self._tool(run_id, "tracker.get_issue", {"key": story_key}, lambda: tracker.get_issue(story_key))
        self.store.update_run(run_id, story_key=story_key, story_json=story)
        ev("story", f"Read {story_key} via {getattr(tracker, 'kind', 'tracker')}: {story['fields']['summary']}")
        if run0.get("repository"):
            repo = run0["repository"]
            ctx = self._tool(run_id, "git.repo_context", {"repo": repo}, lambda: git.repo_context(repo))
            self.store.update_run(run_id, repo_context_json=ctx)
            ev("repository", f"Read-only repository context {repo}@{ctx['ref']} via {getattr(git, 'kind', 'git')}: "
                             f"{len(ctx['top_level'])} top-level entries (context only; not sent to the model in C0)")
        desc = story["fields"]["description"]
        labels = story["fields"].get("labels") or []
        data_class = "synthetic" if ("synthetic" in labels and env.get("data_class") == "synthetic"
                                     and getattr(tracker, "kind", "") == "mock_jira") else "unclassified"
        # Stories from a real tracker are never auto-classified synthetic by label: model input stays gated (fail closed).
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
                              catalog_version=catalog["catalog_version"], plan_created_at=now())
        plan_hash = EP.capture(self, run_id, env)
        self.store.update_run(run_id, status="awaiting_approval")
        cats = {c: sum(t["category"] == c for t in plan["test_cases"]) for c in P.CATEGORIES}
        ev("ac", f"[{model.label}] {len(ac['acceptance_criteria'])} acceptance criteria, each traced to verbatim story text")
        ev("plan", f"[{model.label}] Test plan: {len(plan['test_cases'])} cases {cats}, {len(gaps)} coverage gap(s) (sha256 {plan_hash[:12]})")
        ev("approval", "Waiting for human approval. Execution is blocked until the exact plan is approved.")

    def _model_plan(self, run_id, mctx, desc, oracles):
        feedback, attempt = None, 0
        model = self._run_models.get(run_id, self.model)
        pname = model.name
        if pname in ("ollama", "lmstudio", "llamacpp", "vllm"):
            model.prepare()
            self.store.update_run(run_id, model_version=model.version)
        while True:
            billable = getattr(model, "per_attempt_budget", False)
            if billable:
                ledger = _BudgetLedger(self, run_id, pname, getattr(model, "model", pname), "plan_story", attempt)
                call = lambda: model.generate("plan_story", mctx, P.PLAN_STORY_SCHEMA, feedback, run_id=run_id, budget=ledger)
            else:
                est = model.estimate_max_cost(len(json.dumps(P.build_messages("plan_story", mctx, feedback))))
                if not getattr(model, "cost_free", False):
                    self._budget_check(run_id, est)
                call = lambda: model.generate("plan_story", mctx, P.PLAN_STORY_SCHEMA, feedback, run_id=run_id)
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
                    self.store.add_usage(run_id, pname, getattr(model, "model", pname), "plan_story", attempt, None, est, False, "usage_missing")
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
            self.store.update_run(run_id, model_id=resp.model, model_version=getattr(resp, "version", None), model_label=resp.label)
            self.store.telemetry(run_id, "model_performance", pname, json.dumps({"model": resp.model, **getattr(resp, "metrics", {})}))
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

    def _verify_package(self, run, env=None):
        try:
            if self.review_only and env is None:
                # Reviewing an offline synthetic plan need not start its SUT. Only
                # the explicit loopback harness target may differ by ephemeral port;
                # dispatch still verifies the actual resolved target independently.
                proposed = (run.get("execution_envelope_json") or {}).get("environment", {})
                from .environments import _is_loopback
                from urllib.parse import urlparse
                if proposed.get("kind") == "synthetic" and _is_loopback(urlparse(proposed.get("base_url", "")).hostname or ""):
                    declared = self.envs._base(run["environment"])
                    declared["base_url"] = proposed["base_url"]
                    if declared != proposed or self.envs.problems(proposed):
                        raise EP.PackageChanged("review target is not a registered synthetic environment")
                    env = proposed
            return EP.verify(self, run, env or self.envs.resolve(run["environment"]))
        except (EP.PackageChanged, OSError, ValueError) as exc:
            self.gateway.record(run["id"], "execution.package", {"approval_hash": run.get("plan_hash")},
                                "deny", str(exc))
            raise ConflictError(str(exc)) from exc

    def _execute(self, run_id):
        run = self.store.get_run(run_id)
        ev = lambda s, m: self.store.event(run_id, s, m)
        try:
            valid_hash = EP.digest(run) == run["plan_hash"]
        except EP.PackageChanged:
            valid_hash = False
        if not self.approvals.is_approved(run_id, run["plan_hash"]) or not valid_hash:
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
        try:
            oracle_manifest, fixture_bytes = self._verify_package(run, env)
        except ConflictError as exc:
            self.store.update_run(run_id, status="blocked", error=str(exc))
            ev("blocked", str(exc))
            return
        ex = PlaywrightExecutor(self.gateway, self.artifacts, self.skills, oracle_manifest["oracles"], ROOT / self.cfg["fixture_data_dir"], self.store,
                                timeouts=self.exec_cfg, is_cancelled=self.is_cancelled, fixture_bytes=fixture_bytes)
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
