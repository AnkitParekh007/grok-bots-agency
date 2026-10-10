"""Agent workspace: FastAPI JSON API + single-page chat UI. Bind to 127.0.0.1 only."""
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from . import ROOT
from .runtime import QAAgent


class ChatIn(BaseModel):
    message: str
    environment: str | None = None


class ApprovalIn(BaseModel):
    approver: str
    decision: str
    comment: str = ""


def create_app(data_dir: Path | None = None) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("QA_DATA_DIR", ROOT / "data"))
    agent = QAAgent(data_dir)
    app = FastAPI(title="Bluemeteor QA Agent (Milestone A)")
    app.state.agent = agent

    def run_or_404(run_id):
        r = agent.store.get_run(run_id)
        if not r:
            raise HTTPException(404, "run not found")
        return r

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (ROOT / "qa_agent/static/index.html").read_text()

    @app.get("/api/config")
    def config():
        return {"model_label": agent.model.label, "model_provider": agent.model.name,
                "environments": list(agent.envs.envs), "skills": agent.skills.list()}

    @app.post("/api/chat")
    def chat(body: ChatIn):
        run_id = agent.start_run(body.message, body.environment)
        return {"run_id": run_id}

    @app.get("/api/runs")
    def runs():
        return agent.store.list_runs()

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        r = run_or_404(run_id)
        r["events"] = agent.store.events(run_id)
        r["results"] = agent.store.results(run_id)
        r["approval"] = agent.approvals.latest(run_id)
        r["audit_count"] = len(agent.store.audit_rows(run_id))
        return r

    @app.post("/api/runs/{run_id}/approval")
    def approval(run_id: str, body: ApprovalIn):
        run_or_404(run_id)
        try:
            agent.approve(run_id, body.approver, body.decision, body.comment)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    def send(run_id, rel, media=None):
        run_or_404(run_id)
        try:
            p = agent.artifacts.path_for(run_id, rel)
        except ValueError:
            raise HTTPException(400, "bad path")
        if not p.is_file():
            raise HTTPException(404, "artifact not found")
        return FileResponse(p, media_type=media)

    @app.get("/api/runs/{run_id}/report.html")
    def report_html(run_id: str):
        return send(run_id, "report.html", "text/html")

    @app.get("/api/runs/{run_id}/report.json")
    def report_json(run_id: str):
        r = send(run_id, "report.json", "application/json")
        r.headers["Content-Disposition"] = f'attachment; filename="{run_id}-report.json"'
        return r

    @app.get("/api/runs/{run_id}/audit.jsonl")
    def audit(run_id: str):
        return send(run_id, "audit.jsonl", "application/x-ndjson")

    @app.get("/api/runs/{run_id}/artifacts/{rel:path}")
    def artifact(run_id: str, rel: str):
        return send(run_id, rel)

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("QA_AGENT_PORT", 8600)))


if __name__ == "__main__":
    main()
