"""Milestone B1 demo, fully local: synthetic SUT + LOCAL model stub (no real model) + agent in local-auth mode.
Drives the real UI with Playwright as two users (requester, approver) and saves screenshots to docs/screenshots/b1_*.png.

  .venv/bin/python scripts/demo_b1.py [STORY_KEY]      (default PCC-DEMO-3, the prompt-injection story)

The XAI_API_KEY used here is a FAKE placeholder sent only to the 127.0.0.1 stub. Pricing values are FAKE demo
placeholders so the cost arithmetic is visible; they are not xAI prices."""
import copy, json, os, subprocess, sys, time
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from qa_agent.auth import UserStore  # noqa: E402

SHOTS, DATA = ROOT / "docs/screenshots", ROOT / "data"
STORY = sys.argv[1] if len(sys.argv) > 1 else "PCC-DEMO-3"


def up(url):
    for _ in range(150):
        try:
            if httpx.get(url).status_code < 500:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise SystemExit(f"{url} did not start")


def main():
    SHOTS.mkdir(parents=True, exist_ok=True); DATA.mkdir(exist_ok=True)
    us = UserStore(DATA / "users.json")
    req_tok, appr_tok = us.add("ankit", "requester"), us.add("qa_lead", "approver")
    cfg = json.loads((ROOT / "config/agent.json").read_text())
    cfg["model"]["provider"] = "grok"
    cfg["providers"]["grok"].update(enabled=True, base_url="http://127.0.0.1:8701/v1",
                                    pricing={"input_per_mtok_usd": 2.0, "cached_input_per_mtok_usd": 0.5, "output_per_mtok_usd": 6.0},
                                    _pricing_note="FAKE demo placeholder prices")
    (DATA / "demo_b1_agent_config.json").write_text(json.dumps(cfg, indent=2))
    env = dict(os.environ, SUT_DEFECTS=os.environ.get("SUT_DEFECTS", "all"), SYNTHETIC_SUT_URL="http://127.0.0.1:8601",
               QA_AUTH_MODE="local", QA_USERS_FILE=str(DATA / "users.json"), QA_AGENT_CONFIG=str(DATA / "demo_b1_agent_config.json"),
               XAI_API_KEY="xai-LOCALSTUBFAKEKEY-not-a-real-credential")
    procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "sut.app:app", "--host", "127.0.0.1", "--port", "8601"], cwd=ROOT, env=env),
             subprocess.Popen([sys.executable, "-m", "uvicorn", "qa_agent.devtools.model_stub:app", "--host", "127.0.0.1", "--port", "8701"], cwd=ROOT, env=env),
             subprocess.Popen([sys.executable, "-m", "qa_agent.server"], cwd=ROOT, env=env)]
    try:
        up("http://127.0.0.1:8601/api/health"); up("http://127.0.0.1:8701/_stub/requests"); up("http://127.0.0.1:8600/api/me")
        css = "body{height:auto!important} #main{height:auto} #chat{overflow:visible!important}"
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            req = b.new_context(viewport={"width": 1400, "height": 1000}).new_page()
            req.goto("http://127.0.0.1:8600/"); req.add_style_tag(content=css)
            req.wait_for_selector("#tok"); req.fill("#tok", req_tok); req.click("#login")
            req.wait_for_selector("#msg")
            req.fill("#msg", f"Read {STORY}, generate test cases, execute the approved tests, and give me the results.")
            req.click("#send")
            req.wait_for_selector("#gaps", timeout=30000)
            req.screenshot(path=str(SHOTS / "b1_01_requester_plan_with_gaps.png"), full_page=True)
            rid = req.inner_text("#chat .card b").split()[-1]
            appr = b.new_context(viewport={"width": 1400, "height": 1000}).new_page()
            appr.goto("http://127.0.0.1:8600/"); appr.add_style_tag(content=css)
            appr.wait_for_selector("#tok"); appr.fill("#tok", appr_tok); appr.click("#login")
            appr.wait_for_selector(f"text={rid}"); appr.click(f"text={rid}")
            appr.wait_for_selector("#approve")
            appr.screenshot(path=str(SHOTS / "b1_02_approver_review.png"), full_page=True)
            appr.click("#approve")
            appr.wait_for_selector("#links", timeout=180000)
            appr.screenshot(path=str(SHOTS / "b1_03_results.png"), full_page=True)
            b.close()
        H = {"Authorization": f"Bearer {appr_tok}"}
        run = httpx.get(f"http://127.0.0.1:8600/api/runs/{rid}", headers=H).json()
        print(json.dumps({"run_id": rid, "story": STORY, "status": run["status"], "model_label": run["model_label"],
                          "summary": run["summary_json"], "usage": run["usage"], "approval": {k: run["approval"][k] for k in ("approver", "identity", "auth_mode")},
                          "coverage_gaps": run["planning_json"]["coverage_gaps"],
                          "stub_requests": len(httpx.get("http://127.0.0.1:8701/_stub/requests").json()),
                          "audit_verify": httpx.get("http://127.0.0.1:8600/api/audit/verify", headers=H).json(),
                          "report": str(DATA / "runs" / rid / "report.html")}, indent=2))
    finally:
        for p in procs:
            p.terminate(); p.wait(10)


if __name__ == "__main__":
    main()
