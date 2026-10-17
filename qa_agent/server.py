"""Agent workspace: FastAPI JSON API + single-page chat UI. Bind to 127.0.0.1 only.

Auth modes (config auth.mode or env QA_AUTH_MODE):
  demo  - no authentication; approver name is a self-asserted LABEL, recorded as unauthenticated.
  local - bearer token (header or HttpOnly SameSite=Strict cookie via /api/login) checked against a hashed users file;
          roles viewer/requester/approver enforced per endpoint.
"""
import os
import hmac
from contextlib import asynccontextmanager
from dataclasses import asdict
from urllib.parse import urlsplit
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Request, Response, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.exceptions import RequestValidationError
from . import ROOT
from .auth import Principal, UserStore, has_role
from .runtime import QAAgent, ConflictError
from .store import verify_chain
from .providers.models import ProviderNotApproved
from .sessions import SessionManager
from .oidc import OidcClient, OidcError
from .security import RateLimits, SecurityMiddleware
from .util import register_secret_env
from .observability import Observability, RequestObservability


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=20000)
    environment: str | None = None
    project: str | None = None
    story_key: str | None = None
    repository: str | None = None
    model_provider: str | None = None
    model_id: str | None = None


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approver: str = ""
    decision: str
    comment: str = ""
    plan_hash: str | None = None


class LoginIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class ConnectionScopeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt: str = Field(pattern="^[a-f0-9]{64}$")
    resources: list[str] = Field(min_length=1, max_length=500)
    previous_revision: str | None = None


class ConnectionDiscoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionRevokeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    previous_revision: str = Field(pattern="^[a-f0-9]{64}$")


class JiraWriteProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(create|comment|transition)$")
    target: str | None = Field(default=None, max_length=60)
    transition: str | None = Field(default=None, max_length=80)


class JiraWriteReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_hash: str = Field(pattern="^[a-f0-9]{64}$")
    decision: str = Field(pattern="^(approve|reject)$")


def create_app(data_dir: Path | None = None, auth_mode: str | None = None, users_file: Path | None = None, agent_cfg: dict | None = None, oidc_transport=None) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("QA_DATA_DIR", ROOT / "data"))
    if agent_cfg is None and os.environ.get("QA_AGENT_CONFIG"):
        import json
        agent_cfg = json.loads(Path(os.environ["QA_AGENT_CONFIG"]).read_text())
    agent = QAAgent(data_dir, agent_cfg=agent_cfg)
    acfg = agent.cfg.get("auth", {})
    mode = auth_mode or os.environ.get("QA_AUTH_MODE") or acfg.get("mode", "demo")
    if mode not in ("demo", "local", "oidc"):
        raise ValueError("auth mode must be demo|local|oidc")
    shared = agent.shared
    public_url = agent.cfg.get("deployment", {}).get("public_url", "")
    if shared and (mode != "oidc" or urlsplit(public_url).scheme != "https" or not urlsplit(public_url).hostname):
        raise ValueError("shared profile requires OIDC and an explicit HTTPS public URL")
    if mode == "oidc" and (urlsplit(public_url).scheme != "https" or not urlsplit(public_url).hostname):
        raise ValueError("OIDC requires an explicit HTTPS workspace origin")
    if mode == "demo" and (getattr(agent.tracker, "kind", "") != "mock_jira" or getattr(agent.git, "kind", "") != "synthetic_git"):
        raise ValueError("real organization integrations require authenticated workspace mode")
    if mode == "demo" and any(c.get("enabled") for c in agent.connections.cfg.values()):
        raise ValueError("organization connections require authenticated workspace mode")
    if shared and (urlsplit(public_url).path not in ("", "/") or urlsplit(public_url).username or urlsplit(public_url).query):
        raise ValueError("workspace public URL must be a plain HTTPS origin")
    if shared and not (acfg.get("oidc", {}).get("required_amr") or acfg.get("oidc", {}).get("acr_values")):
        raise ValueError("shared OIDC profile requires an explicit MFA/authentication-strength claim policy")
    users = UserStore(Path(users_file or os.environ.get("QA_USERS_FILE") or (ROOT / acfg.get("users_file", "data/users.json"))))
    @asynccontextmanager
    async def lifespan(app):
        try:
            agent.recover_pending()
            yield
        finally:
            agent.close()
            if oidc is not None:
                oidc.close()
            agent.connections.close()

    app = FastAPI(title="Bluemeteor QA Agent", lifespan=lifespan)
    app.state.agent, app.state.auth_mode = agent, mode
    session_cfg = acfg.get("sessions", {})
    register_secret_env(session_cfg.get("key_env", "QA_SESSION_ENCRYPTION_KEY"))
    if mode == "oidc":
        register_secret_env(acfg.get("oidc", {}).get("flow_key_env", "QA_OIDC_FLOW_KEY"))
        register_secret_env(acfg.get("oidc", {}).get("client_secret_env"))
    session_key = os.environ.get(session_cfg.get("key_env", "QA_SESSION_ENCRYPTION_KEY"))
    sessions = SessionManager(agent.store, users, key=session_key, shared=shared,
                              idle_s=session_cfg.get("idle_s", 900), lifetime_s=session_cfg.get("lifetime_s", 3600))
    app.state.sessions = sessions
    limiter = RateLimits(agent.store)
    session_cookie = "__Host-qa_session" if shared else "qa_session"
    csrf_cookie = "__Host-qa_csrf" if shared else "qa_csrf"
    oidc = OidcClient(acfg["oidc"], agent.store, transport=oidc_transport) if mode == "oidc" else None
    from .job_authorization import JobAuthorization
    job_authorization = JobAuthorization(agent, sessions, users, mode, forbid_self_approval=acfg.get("forbid_self_approval", True))
    from .jira_writes import JiraWrites, JiraWriteDenied
    jira_writes = JiraWrites(agent, job_authorization)
    app.state.jira_writes = jira_writes
    if mode == "demo" and any(c.get("enabled") for c in agent.cfg.get("jira_writes", {}).values()):
        raise ValueError("Jira writes require authenticated human workspace mode")
    if agent.queue:
        agent.queue.authorizer = job_authorization
    if oidc and public_url and acfg["oidc"]["redirect_uri"] != public_url.rstrip("/") + "/api/oidc/callback":
        raise ValueError("OIDC callback must match the configured workspace public URL")
    if mode == "oidc":
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(public_url).hostname])
    else:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    observer = Observability(agent, agent.cfg.get("observability"))
    app.state.observer = observer
    metrics_env = observer.cfg.get("metrics_token_env", "QA_METRICS_TOKEN")
    if not isinstance(metrics_env, str) or not metrics_env.isidentifier() or len(metrics_env) > 128:
        raise ValueError("metrics credential must be a server-side environment reference")
    register_secret_env(metrics_env)
    app.mount("/static", StaticFiles(directory=ROOT / "qa_agent/static"), name="static")

    def principal(request: Request) -> Principal:
        if mode == "demo":
            return Principal(name=None, role="approver", mode="demo")
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            p = users.verify(h[7:]) if mode == "local" else None
        else:
            payload = sessions.verify(request.cookies.get(session_cookie))
            try:
                p = Principal(**payload) if payload else None
            except TypeError:
                p = None
        if not p:
            raise HTTPException(401, "authentication required")
        if p.mode == "oidc" and p.authorization_revision != agent.access.revision:
            sessions.revoke(request.cookies.get(session_cookie))
            raise HTTPException(401, "access policy changed; sign in again")
        return p

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": [{k: e[k] for k in ("loc", "msg", "type")} for e in exc.errors()]}, 422)

    from .storage import StorageLimitExceeded
    @app.exception_handler(StorageLimitExceeded)
    async def storage_unavailable(request, exc):
        return JSONResponse({"detail": "evidence storage admission unavailable; operator review required"}, 503)

    @app.middleware("http")
    async def browser_mutation_boundary(request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            expected = public_url.rstrip("/") if public_url else str(request.base_url).rstrip("/")
            if origin and origin != expected:
                return JSONResponse({"detail": "request origin is not allowed"}, 403)
            cookie = request.cookies.get(session_cookie)
            if cookie and not request.headers.get("authorization") and request.url.path not in ("/api/login", "/api/oidc/backchannel"):
                if shared and origin != expected:
                    return JSONResponse({"detail": "cookie mutations require the workspace origin"}, 403)
                if not sessions.csrf_ok(cookie, request.headers.get("x-csrf-token"), request.cookies.get(csrf_cookie)):
                    return JSONResponse({"detail": "invalid session or CSRF token"}, 403)
        return await call_next(request)

    def throttle(request, scope="login"):
        retry = limiter.consume(scope, request.client.host if request.client else "unknown", acfg.get("login_attempts_per_minute", 20))
        if retry:
            agent.gateway.record(None, "http.auth_throttle", {"scope": scope}, "deny", "authentication rate limit")
            raise HTTPException(429, "authentication rate limit", headers={"Retry-After": str(retry)})

    def set_session(response, p, **kwargs):
        issued = sessions.issue(asdict(p), **kwargs)
        max_age = max(1, int(issued["expires"] - sessions.clock()))
        response.set_cookie(session_cookie, issued["token"], httponly=True, secure=shared or mode == "oidc", samesite="strict", max_age=max_age)
        response.set_cookie(csrf_cookie, issued["csrf"], httponly=False, secure=shared or mode == "oidc", samesite="strict", max_age=max_age)

    def need(role):
        def dep(p: Principal = Depends(principal)):
            if not has_role(p, role):
                agent.gateway.record(None, "http.authz", {"role_required": role, "user": p.name}, "deny", f"role {p.role} < {role}")
                raise HTTPException(403, f"role '{role}' required")
            return p
        return dep

    def run_or_404(run_id):
        r = agent.store.get_run(run_id)
        if not r:
            raise HTTPException(404, "run not found")
        return r

    def can_access(p, run):
        """Run-level access. demo mode: single trusted local user, no per-run checks.
        local mode: the run's requester (owner) or any approver; everyone else gets 404 (existence not disclosed)."""
        if mode == "demo":
            return True
        if run.get("tenant_id", "local") != p.tenant_id:
            return False
        project = run.get("project")
        if project and not agent.access.allows(p, project):
            return False
        if not project and (agent.access.strict or p.project_roles is not None):
            return False
        approver = agent.access.allows(p, project, "approver") if project else has_role(p, "approver")
        return approver or (p.name is not None and run.get("requested_by") == p.name)

    def owned_run(run_id, p, action):
        r = run_or_404(run_id)
        if not can_access(p, r):
            agent.gateway.record(run_id, "http.run_access", {"user": p.name, "action": action}, "deny", "not run owner or approver")
            raise HTTPException(404, "run not found")
        return r

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (ROOT / "qa_agent/static/index.html").read_text(encoding="utf-8")

    @app.post("/api/login")
    def login(body: LoginIn, response: Response, request: Request):
        throttle(request)
        if mode == "oidc":
            raise HTTPException(403, "use organization sign-in")
        p = users.verify(body.token) if mode == "local" else None
        if mode == "local" and not p:
            agent.gateway.record(None, "http.login", {}, "deny", "invalid local credential")
            raise HTTPException(401, "invalid token")
        if p:
            set_session(response, p)
            agent.gateway.record(None, "http.login", {"user": p.name}, "allow", "local session issued")
        return {"user": p.name if p else None, "role": p.role if p else "approver", "mode": mode}

    @app.post("/api/logout")
    def logout(response: Response, request: Request):
        payload = sessions.verify(request.cookies.get(session_cookie))
        sessions.revoke(request.cookies.get(session_cookie))
        agent.gateway.record(None, "http.logout", {"user": payload.get("name") if payload else None}, "allow", "browser session revoked")
        response.delete_cookie(session_cookie, secure=shared or mode == "oidc", httponly=True, samesite="strict")
        response.delete_cookie(csrf_cookie, secure=shared or mode == "oidc", samesite="strict")
        return {"ok": True}

    @app.get("/api/oidc/start")
    def oidc_start(request: Request):
        if oidc is None:
            raise HTTPException(404, "organization sign-in is not configured")
        throttle(request, "oidc_start")
        flow = oidc.begin()
        response = RedirectResponse(flow["url"], status_code=303)
        response.set_cookie("__Host-qa_oidc_flow", flow["binding"], httponly=True, secure=True, samesite="lax", max_age=300)
        return response

    @app.get("/api/oidc/callback")
    def oidc_callback(request: Request, state: str = "", code: str = ""):
        if oidc is None:
            raise HTTPException(404, "organization sign-in is not configured")
        throttle(request, "oidc_callback")
        try:
            claims = oidc.complete(code, state, request.cookies.get("__Host-qa_oidc_flow"))
            p = agent.access.principal(claims)
            response = RedirectResponse("/", status_code=303)
            set_session(response, p, issuer=claims["iss"], subject=claims["sub"], sid=claims.get("sid"), expires_at=claims["exp"])
            response.delete_cookie("__Host-qa_oidc_flow", secure=True, httponly=True, samesite="lax")
            agent.gateway.record(None, "http.oidc_login", {"user": p.name}, "allow", "verified organization session issued")
            return response
        except ValueError:
            agent.gateway.record(None, "http.oidc_login", {}, "deny", "OIDC callback validation failed")
            raise HTTPException(401, "organization sign-in failed") from None

    @app.post("/api/oidc/backchannel")
    def oidc_logout(request: Request, logout_token: str = Form(..., max_length=32768)):
        if oidc is None:
            raise HTTPException(404, "organization sign-in is not configured")
        throttle(request, "oidc_logout")
        try:
            claims = oidc.consume_logout(logout_token)
            sessions.revoke_external(claims["iss"], subject=claims.get("sub"), sid=claims.get("sid"))
            return {"ok": True}
        except ValueError:
            raise HTTPException(400, "invalid organization logout event") from None

    @app.get("/api/me")
    def me(request: Request):
        try:
            p = principal(request)
        except HTTPException:
            return {"user": None, "role": None, "mode": mode, "authenticated": False, "sign_in_url": "/api/oidc/start" if oidc else None}
        return {"user": p.name, "role": p.role, "mode": p.mode, "authenticated": p.authenticated,
                "display_name": p.display_name, "tenant_id": p.tenant_id, "project_roles": p.project_roles}

    @app.get("/healthz")
    def live():
        return JSONResponse({"alive": True}, headers={"Cache-Control": "no-store"})

    @app.get("/readyz")
    def ready():
        healthy = observer.health()["ready"]
        return JSONResponse({"ready": healthy, "scope": "process"}, 200 if healthy else 503, headers={"Cache-Control": "no-store"})

    def operator_or_scraper(request):
        header = request.headers.get("authorization", "")
        token = os.environ.get(metrics_env, "")
        if token and len(token) >= 32 and header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode(), token.encode()):
            return
        p = principal(request)
        if mode != "demo" and ((agent.access.strict or p.project_roles is not None) and
                p.name not in agent.access.cfg.get("system_auditors", []) or not has_role(p, "approver")):
            raise HTTPException(403, "operator observability permission required")

    @app.get("/api/health")
    def operator_health(request: Request):
        operator_or_scraper(request)
        return observer.health()

    @app.get("/metrics")
    def metrics(request: Request):
        operator_or_scraper(request)
        return Response(observer.prometheus(), media_type="text/plain; version=0.0.4", headers={"Cache-Control": "no-store"})

    @app.get("/api/config")
    def config(p: Principal = Depends(need("viewer"))):
        return {"model_label": agent.model.label, "model_provider": agent.model.name, "auth_mode": mode,
                "environments": [e["name"] for e in agent.envs.list_environments() if e["executable"]], "skills": agent.skills.list(), "budget": agent.budget}

    @app.post("/api/chat")
    def chat(body: ChatIn, request: Request, p: Principal = Depends(need("requester"))):
        if not observer.health()["ready"]:
            raise HTTPException(503, "service admission unavailable; operator recovery required")
        if (agent.access.strict or p.project_roles is not None) and not body.project:
            raise HTTPException(400, "an authorized project is required")
        if body.project:
            _project(body.project, p, "requester")
        try:
            if body.project or body.repository:
                agent.select(body.project, body.story_key, body.repository, body.environment)
        except ValueError as e:
            agent.gateway.record(None, "workspace.select", {"user": p.name, "project": body.project, "repository": body.repository}, "deny", str(e))
            raise HTTPException(400, str(e))
        try:
            return {"run_id": agent.start_run(body.message, body.environment, story_key=body.story_key, requested_by=p.name,
                                              project=body.project, repository=body.repository,
                                              model_provider=body.model_provider, model_id=body.model_id, tenant_id=p.tenant_id,
                                              authorization=job_authorization.receipt(request, p, session_cookie))}
        except (ValueError, ProviderNotApproved) as e:
            agent.gateway.record(None, "model.select", {"provider": body.model_provider, "model": body.model_id}, "deny", str(e))
            raise HTTPException(403, str(e))

    @app.get("/api/models")
    def models(p: Principal = Depends(need("viewer"))):
        return agent.models.list()

    @app.post("/api/models/{provider}/probe")
    def model_probe(provider: str, model_id: str | None = None, p: Principal = Depends(need("requester"))):
        from .providers.models import ProviderNotApproved
        try:
            return agent.models.probe(provider, model_id)
        except (ProviderNotApproved, ValueError) as e:
            raise HTTPException(403, str(e))

    # ---- workspace selection (read-only providers, every call goes through the audited gateway) ----
    from .integrations.http import IntegrationError

    def _project(project, p, role="viewer"):
        pc = agent.projects.get(project)
        if not pc or not agent.access.allows(p, project, role):
            agent.gateway.record(None, "http.project_access", {"user": p.name, "project": project}, "deny", "project scope denied")
            raise HTTPException(404, "project not found")
        return pc

    def _read(tool, args, fn):
        try:
            return agent.gateway.invoke(None, tool, args, fn)
        except IntegrationError as e:
            raise HTTPException(502 if e.status is None else (403 if e.status in (401, 403) else 502), str(e))
        except (KeyError, ValueError) as e:
            raise HTTPException(404 if isinstance(e, KeyError) else 400, str(e).strip("'\""))

    def connection_action(fn):
        try:
            return fn()
        except IntegrationError as e:
            raise HTTPException(e.status if e.status in (404, 409, 429) else (403 if isinstance(e, IntegrationNotAuthorized) else 502), str(e))

    from .integrations.http import IntegrationNotAuthorized

    @app.get("/api/connections")
    def connections(p: Principal = Depends(need("viewer"))):
        return agent.connections.list(p)

    @app.post("/api/connections/{connection_id}/discover")
    def discover_connection(connection_id: str, body: ConnectionDiscoverIn, p: Principal = Depends(need("approver"))):
        return connection_action(lambda: agent.connections.discover(connection_id, p))

    @app.post("/api/connections/{connection_id}/scope")
    def select_connection_scope(connection_id: str, body: ConnectionScopeIn, p: Principal = Depends(need("approver"))):
        return connection_action(lambda: agent.connections.select(connection_id, p, body.receipt, body.resources, body.previous_revision))

    @app.delete("/api/connections/{connection_id}/scope")
    def revoke_connection_scope(connection_id: str, body: ConnectionRevokeIn, p: Principal = Depends(need("approver"))):
        return connection_action(lambda: agent.connections.revoke(connection_id, p, body.previous_revision))

    @app.get("/api/projects")
    def projects(p: Principal = Depends(need("viewer"))):
        return [{"key": k, "name": v.get("name"), "jira_project": v["jira_project"], "repositories": v.get("repositories", []),
                 "environments": v.get("environments", []), "tracker": getattr(agent.tracker, "kind", None),
                 "git": getattr(agent.git, "kind", None)} for k, v in agent.projects.items() if agent.access.allows(p, k)]

    @app.get("/api/projects/{project}/stories")
    def stories(project: str, q: str | None = None, p: Principal = Depends(need("viewer"))):
        jp = _project(project, p)["jira_project"]
        return _read("tracker.search", {"project": jp, "text": q}, lambda: agent.tracker_for(project).search_issues(jp, q, 50))

    @app.get("/api/projects/{project}/stories/{key}")
    def story(project: str, key: str, p: Principal = Depends(need("viewer"))):
        jp = _project(project, p)["jira_project"]
        if not key.startswith(jp + "-"):
            raise HTTPException(400, f"story {key} is not in project {jp}")
        tracker = connection_action(lambda: agent.tracker_for(project))
        issue = _read("tracker.get_issue", {"key": key}, lambda: tracker.get_issue(key))
        return {"key": key, "summary": issue["fields"]["summary"], "labels": issue["fields"].get("labels", []),
                "acceptance_criteria": _read("tracker.acceptance_criteria", {"key": key}, lambda: tracker.acceptance_criteria(key)),
                "comments": _read("tracker.get_comments", {"key": key}, lambda: tracker.get_comments(key)),
                "attachments": _read("tracker.list_attachments", {"key": key}, lambda: tracker.list_attachments(key))}

    @app.get("/api/projects/{project}/repositories")
    def repositories(project: str, p: Principal = Depends(need("viewer"))):
        allowed = set(_project(project, p).get("repositories", []))
        return [r for r in _read("git.list_repos", {}, lambda: agent.git_for(project).list_repositories()) if r["full_name"] in allowed]

    @app.get("/api/projects/{project}/repositories/{owner}/{slug}/context")
    def repository_context(project: str, owner: str, slug: str, p: Principal = Depends(need("viewer"))):
        repo = f"{owner}/{slug}"
        if repo not in _project(project, p).get("repositories", []):
            raise HTTPException(404, "repository not linked to project")
        return _read("git.repo_context", {"repo": repo}, lambda: agent.git_for(project).repo_context(repo))

    @app.get("/api/environments")
    def environments(p: Principal = Depends(need("viewer"))):
        allowed = {e for k, pc in agent.projects.items() if agent.access.allows(p, k) for e in pc.get("environments", [])}
        return [e for e in _read("env.list", {}, agent.envs.list_environments) if e["name"] in allowed]

    @app.get("/api/runs")
    def runs(p: Principal = Depends(need("viewer"))):
        return [r for r in agent.store.list_runs() if can_access(p, r)]

    @app.get("/api/runs/{run_id}")
    def run(run_id: str, p: Principal = Depends(need("viewer"))):
        r = owned_run(run_id, p, "read")
        r["events"] = agent.store.events(run_id)
        r["results"] = agent.store.results(run_id)
        r["approval"] = agent.approvals.latest(run_id)
        r["audit_count"] = len(agent.store.audit_rows(run_id))
        r["usage"] = agent.store.run_usage(run_id)["totals"]
        r["jobs"] = agent.store.q("SELECT kind,state,attempts,created,started,finished,error FROM jobs WHERE run_id=? ORDER BY created", (run_id,))
        return r

    @app.post("/api/runs/{run_id}/approval")
    def approval(run_id: str, body: ApprovalIn, request: Request, p: Principal = Depends(need("approver"))):
        if not observer.health()["ready"]:
            raise HTTPException(503, "service admission unavailable; operator recovery required")
        run = owned_run(run_id, p, "approve")   # approvers may act on any run (but never self-approve, below)
        if mode != "demo":
            if p.kind != "human":
                raise HTTPException(403, "manual approval requires a human identity")
            if run.get("project") and not agent.access.allows(p, run["project"], "approver"):
                raise HTTPException(403, "project approver role required")
            if not body.plan_hash:
                raise HTTPException(400, "plan_hash required: approve the exact plan you reviewed")
            if acfg.get("forbid_self_approval", True) and run.get("requested_by") == p.name:
                agent.gateway.record(run_id, "plan.approve", {"user": p.name}, "deny", "self-approval forbidden")
                raise HTTPException(403, "requester cannot approve their own run")
            approver, identity, label = p.name, p.name, body.approver or None
        else:
            if not body.approver:
                raise HTTPException(400, "approver label required in demo mode")
            approver, identity, label = body.approver, None, body.approver
        try:
            agent.approve(run_id, approver, body.decision, body.comment, identity=identity, auth_mode=mode,
                          plan_hash=body.plan_hash, approver_label=label,
                          authorization=job_authorization.receipt(request, p, session_cookie))
        except ValueError as e:   # includes ConflictError (stale/duplicate/busy) and bad decision values
            raise HTTPException(409, str(e))
        return {"ok": True, "authenticated": identity is not None}

    def jira_write_action(run_id, request, p, action, *, admit=True):
        owned_run(run_id, p, "jira_write")
        if not p.authenticated or p.kind != "human" or mode == "demo":
            raise HTTPException(403, "Jira writes require an authenticated human")
        if admit and not observer.health()["ready"]:
            raise HTTPException(503, "service admission unavailable; operator recovery required")
        try:
            receipt = job_authorization.receipt(request, p, session_cookie)
            return action(receipt)
        except JiraWriteDenied as e:
            agent.gateway.record(run_id, "jira.write.denied", {}, "deny", str(e))
            raise HTTPException(409, str(e)) from None
        except (ValueError, KeyError, TypeError, OSError):
            raise HTTPException(409, "Jira write configuration or source contract is unavailable") from None
        except Exception:
            # Upstream errors may contain credentials/customer text. Return no body detail.
            raise HTTPException(503, "Jira write unavailable; inspect its durable operation receipt") from None

    @app.post("/api/runs/{run_id}/jira-writes")
    def propose_jira_write(run_id: str, body: JiraWriteProposalIn, request: Request, p: Principal = Depends(need("requester"))):
        return jira_write_action(run_id, request, p, lambda receipt: jira_writes.propose(
            run_id, body.kind, receipt, target=body.target, transition=body.transition))

    @app.get("/api/runs/{run_id}/jira-writes")
    def list_jira_writes(run_id: str, request: Request, p: Principal = Depends(need("requester"))):
        return jira_write_action(run_id, request, p, lambda receipt: jira_writes.list(run_id, receipt), admit=False)

    @app.get("/api/runs/{run_id}/jira-writes/{operation_id}")
    def get_jira_write(run_id: str, operation_id: str, request: Request, p: Principal = Depends(need("requester"))):
        return jira_write_action(run_id, request, p, lambda receipt: jira_writes.get(run_id, operation_id, receipt), admit=False)

    @app.post("/api/runs/{run_id}/jira-writes/{operation_id}/review")
    def review_jira_write(run_id: str, operation_id: str, body: JiraWriteReviewIn, request: Request, p: Principal = Depends(need("approver"))):
        return jira_write_action(run_id, request, p, lambda receipt: jira_writes.review(
            run_id, operation_id, receipt, body.draft_hash, body.decision))

    @app.post("/api/runs/{run_id}/jira-writes/{operation_id}/dispatch")
    def dispatch_jira_write(run_id: str, operation_id: str, request: Request, p: Principal = Depends(need("approver"))):
        return jira_write_action(run_id, request, p, lambda receipt: jira_writes.dispatch(run_id, operation_id, receipt))

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str, p: Principal = Depends(need("requester"))):
        run = run_or_404(run_id)   # existing behaviour: 403 (audited) for non-owner requesters
        if mode != "demo" and (run.get("tenant_id", "local") != p.tenant_id or
              (run.get("project") and not agent.access.allows(p, run["project"], "requester")) or
              (not run.get("project") and (agent.access.strict or p.project_roles is not None))):
            raise HTTPException(404, "run not found")
        project_approver = agent.access.allows(p, run["project"], "approver") if run.get("project") else has_role(p, "approver")
        if mode != "demo" and not project_approver and run.get("requested_by") != p.name:
            agent.gateway.record(run_id, "run.cancel", {"user": p.name}, "deny", "not the run owner")
            raise HTTPException(403, "only the run owner or an approver can cancel this run")
        try:
            agent.cancel(run_id, by=p.name or "demo-user")
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.get("/api/runs/{run_id}/usage")
    def usage(run_id: str, p: Principal = Depends(need("viewer"))):
        owned_run(run_id, p, "usage")
        return agent.store.run_usage(run_id)

    # ---- server-side eval grants (synthetic eval only; local auth only; creator identity from session) ----
    @app.post("/api/eval-grants")
    def create_eval_grant(body: dict, p: Principal = Depends(need("approver"))):
        from .eval_authorization import create_grant, AuthorizationError
        if mode != "local":
            raise HTTPException(403, "eval grants require local authentication mode")
        try:
            return create_grant(agent.store, agent.gateway, p, body)   # any identity fields in body are ignored
        except AuthorizationError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/eval-grants")
    def list_eval_grants(p: Principal = Depends(need("approver"))):
        if mode != "local":
            raise HTTPException(403, "eval grants require local authentication mode")
        return agent.store.q("SELECT * FROM eval_grants ORDER BY created_at")

    @app.post("/api/eval-grants/{grant_id}/revoke")
    def revoke_eval_grant(grant_id: str, p: Principal = Depends(need("approver"))):
        from .eval_authorization import revoke_grant, AuthorizationError
        if mode != "local":
            raise HTTPException(403, "eval grants require local authentication mode")
        try:
            revoke_grant(agent.store, agent.gateway, p, grant_id)
        except AuthorizationError as e:
            raise HTTPException(409, str(e))
        return {"revoked": grant_id}

    @app.get("/api/telemetry")
    def telemetry(p: Principal = Depends(need("approver" if mode == "local" else "viewer"))):   # cross-run data
        if (agent.access.strict or p.project_roles is not None) and p.name not in agent.access.cfg.get("system_auditors", []):
            raise HTTPException(403, "system auditor permission required for cross-project telemetry")
        return {"by_kind": agent.store.telemetry_summary(), "day_spend_usd": agent.store.day_spend()}

    @app.get("/api/audit/verify")
    def audit_verify(p: Principal = Depends(need("viewer"))):
        if (agent.access.strict or p.project_roles is not None) and p.name not in agent.access.cfg.get("system_auditors", []):
            raise HTTPException(403, "system auditor permission required")
        return agent.store.verify_audit_chain()

    @app.get("/api/audit/export")
    def audit_export(p: Principal = Depends(need("approver" if mode == "local" else "viewer"))):   # cross-run data
        if (agent.access.strict or p.project_roles is not None) and p.name not in agent.access.cfg.get("system_auditors", []):
            raise HTTPException(403, "system auditor permission required")
        out = data_dir / "audit_export.jsonl"
        agent.store.export_audit(out)
        r = FileResponse(out, media_type="application/x-ndjson")
        r.headers["Content-Disposition"] = 'attachment; filename="audit_export.jsonl"'
        return r

    def send(run_id, rel, media=None, p=None):
        owned_run(run_id, p, f"artifact:{rel}")
        try:
            path = agent.artifacts.path_for(run_id, rel, create=False)
        except ValueError:
            raise HTTPException(400, "bad path")
        if path == agent.artifacts.path_for(run_id, "inputs/config.json", create=False) and \
                p.name not in agent.access.cfg.get("system_auditors", []):
            raise HTTPException(404, "artifact not found")
        if not path.is_file():
            raise HTTPException(404, "artifact not found")
        return FileResponse(path, media_type=media)

    @app.get("/api/runs/{run_id}/report.html")
    def report_html(run_id: str, p: Principal = Depends(need("viewer"))):
        return send(run_id, "report.html", "text/html", p)

    @app.get("/api/runs/{run_id}/report.json")
    def report_json(run_id: str, p: Principal = Depends(need("viewer"))):
        r = send(run_id, "report.json", "application/json", p)
        r.headers["Content-Disposition"] = f'attachment; filename="{run_id}-report.json"'
        return r

    @app.get("/api/runs/{run_id}/audit.jsonl")
    def audit(run_id: str, p: Principal = Depends(need("viewer"))):
        return send(run_id, "audit.jsonl", "application/x-ndjson", p)

    @app.get("/api/runs/{run_id}/artifacts/{rel:path}")
    def artifact(run_id: str, rel: str, p: Principal = Depends(need("viewer"))):
        return send(run_id, rel, None, p)

    app.add_middleware(SecurityMiddleware, shared=shared, **agent.cfg.get("http_limits", {}))
    app.add_middleware(RequestObservability, observer=observer)
    return app


def main():
    import uvicorn
    import logging
    logger = logging.getLogger("qa_agent.access")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("QA_AGENT_PORT", 8600)), access_log=False)


if __name__ == "__main__":
    main()
