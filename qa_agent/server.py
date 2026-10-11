"""Agent workspace: FastAPI JSON API + single-page chat UI. Bind to 127.0.0.1 only.

Auth modes (config auth.mode or env QA_AUTH_MODE):
  demo  - no authentication; approver name is a self-asserted LABEL, recorded as unauthenticated.
  local - bearer token (header or HttpOnly SameSite=Strict cookie via /api/login) checked against a hashed users file;
          roles viewer/requester/approver enforced per endpoint.
"""
import os
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from . import ROOT
from .auth import Principal, UserStore, has_role
from .runtime import QAAgent, ConflictError
from .store import verify_chain


class ChatIn(BaseModel):
    message: str
    environment: str | None = None


class ApprovalIn(BaseModel):
    approver: str = ""
    decision: str
    comment: str = ""
    plan_hash: str | None = None


class LoginIn(BaseModel):
    token: str


def create_app(data_dir: Path | None = None, auth_mode: str | None = None, users_file: Path | None = None, agent_cfg: dict | None = None) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("QA_DATA_DIR", ROOT / "data"))
    if agent_cfg is None and os.environ.get("QA_AGENT_CONFIG"):
        import json
        agent_cfg = json.loads(Path(os.environ["QA_AGENT_CONFIG"]).read_text())
    agent = QAAgent(data_dir, agent_cfg=agent_cfg)
    acfg = agent.cfg.get("auth", {})
    mode = auth_mode or os.environ.get("QA_AUTH_MODE") or acfg.get("mode", "demo")
    if mode not in ("demo", "local"):
        raise ValueError("auth mode must be demo|local")
    users = UserStore(Path(users_file or os.environ.get("QA_USERS_FILE") or (ROOT / acfg.get("users_file", "data/users.json"))))
    app = FastAPI(title="Bluemeteor QA Agent (Milestone B1)")
    app.state.agent, app.state.auth_mode = agent, mode

    def principal(request: Request) -> Principal:
        if mode == "demo":
            return Principal(name=None, role="approver", mode="demo")
        h = request.headers.get("authorization", "")
        tok = h[7:] if h.lower().startswith("bearer ") else request.cookies.get("qa_session")
        p = users.verify(tok)
        if not p:
            raise HTTPException(401, "authentication required")
        return p

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

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (ROOT / "qa_agent/static/index.html").read_text()

    @app.post("/api/login")
    def login(body: LoginIn, response: Response):
        p = users.verify(body.token) if mode == "local" else None
        if mode == "local" and not p:
            raise HTTPException(401, "invalid token")
        if p:
            response.set_cookie("qa_session", body.token, httponly=True, samesite="strict")
        return {"user": p.name if p else None, "role": p.role if p else "approver", "mode": mode}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie("qa_session")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        try:
            p = principal(request)
        except HTTPException:
            return {"user": None, "role": None, "mode": mode, "authenticated": False}
        return {"user": p.name, "role": p.role, "mode": p.mode, "authenticated": p.authenticated}

    @app.get("/api/config")
    def config(p: Principal = Depends(need("viewer"))):
        return {"model_label": agent.model.label, "model_provider": agent.model.name, "auth_mode": mode,
                "environments": list(agent.envs.envs), "skills": agent.skills.list(), "budget": agent.budget}

    @app.post("/api/chat")
    def chat(body: ChatIn, p: Principal = Depends(need("requester"))):
        return {"run_id": agent.start_run(body.message, body.environment, requested_by=p.name)}

    @app.get("/api/runs")
    def runs(p: Principal = Depends(need("viewer"))):
        return agent.store.list_runs()

    @app.get("/api/runs/{run_id}")
    def run(run_id: str, p: Principal = Depends(need("viewer"))):
        r = run_or_404(run_id)
        r["events"] = agent.store.events(run_id)
        r["results"] = agent.store.results(run_id)
        r["approval"] = agent.approvals.latest(run_id)
        r["audit_count"] = len(agent.store.audit_rows(run_id))
        r["usage"] = agent.store.run_usage(run_id)["totals"]
        return r

    @app.post("/api/runs/{run_id}/approval")
    def approval(run_id: str, body: ApprovalIn, p: Principal = Depends(need("approver"))):
        run = run_or_404(run_id)
        if mode == "local":
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
                          plan_hash=body.plan_hash, approver_label=label)
        except ValueError as e:   # includes ConflictError (stale/duplicate/busy) and bad decision values
            raise HTTPException(409, str(e))
        return {"ok": True, "authenticated": identity is not None}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str, p: Principal = Depends(need("requester"))):
        run = run_or_404(run_id)
        if mode == "local" and not has_role(p, "approver") and run.get("requested_by") != p.name:
            agent.gateway.record(run_id, "run.cancel", {"user": p.name}, "deny", "not the run owner")
            raise HTTPException(403, "only the run owner or an approver can cancel this run")
        try:
            agent.cancel(run_id, by=p.name or "demo-user")
        except ConflictError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.get("/api/runs/{run_id}/usage")
    def usage(run_id: str, p: Principal = Depends(need("viewer"))):
        run_or_404(run_id)
        return agent.store.run_usage(run_id)

    @app.get("/api/telemetry")
    def telemetry(p: Principal = Depends(need("viewer"))):
        return {"by_kind": agent.store.telemetry_summary(), "day_spend_usd": agent.store.day_spend()}

    @app.get("/api/audit/verify")
    def audit_verify(p: Principal = Depends(need("viewer"))):
        return agent.store.verify_audit_chain()

    @app.get("/api/audit/export")
    def audit_export(p: Principal = Depends(need("viewer"))):
        out = data_dir / "audit_export.jsonl"
        agent.store.export_audit(out)
        r = FileResponse(out, media_type="application/x-ndjson")
        r.headers["Content-Disposition"] = 'attachment; filename="audit_export.jsonl"'
        return r

    def send(run_id, rel, media=None):
        run_or_404(run_id)
        try:
            path = agent.artifacts.path_for(run_id, rel)
        except ValueError:
            raise HTTPException(400, "bad path")
        if not path.is_file():
            raise HTTPException(404, "artifact not found")
        return FileResponse(path, media_type=media)

    @app.get("/api/runs/{run_id}/report.html")
    def report_html(run_id: str, p: Principal = Depends(need("viewer"))):
        return send(run_id, "report.html", "text/html")

    @app.get("/api/runs/{run_id}/report.json")
    def report_json(run_id: str, p: Principal = Depends(need("viewer"))):
        r = send(run_id, "report.json", "application/json")
        r.headers["Content-Disposition"] = f'attachment; filename="{run_id}-report.json"'
        return r

    @app.get("/api/runs/{run_id}/audit.jsonl")
    def audit(run_id: str, p: Principal = Depends(need("viewer"))):
        return send(run_id, "audit.jsonl", "application/x-ndjson")

    @app.get("/api/runs/{run_id}/artifacts/{rel:path}")
    def artifact(run_id: str, rel: str, p: Principal = Depends(need("viewer"))):
        return send(run_id, rel)

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("QA_AGENT_PORT", 8600)))


if __name__ == "__main__":
    main()
