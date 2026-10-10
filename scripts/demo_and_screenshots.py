"""Headless end-to-end demo through the real workspace UI (Playwright drives the chat page like a user).
Starts SUT + agent on loopback, sends the demo prompt, screenshots plan approval and results,
clicks Approve, waits for real execution, prints the summary and report path."""
import os, subprocess, sys, time, json
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs/screenshots"
PROMPT = "Read the synthetic supplier import story, generate test cases, execute the approved tests, and give me the results."


def up(url):
    for _ in range(100):
        try:
            if httpx.get(url).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise SystemExit(f"{url} did not start")


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, SUT_DEFECTS=os.environ.get("SUT_DEFECTS", "all"), SYNTHETIC_SUT_URL="http://127.0.0.1:8601")
    procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "sut.app:app", "--host", "127.0.0.1", "--port", "8601"], cwd=ROOT, env=env),
             subprocess.Popen([sys.executable, "-m", "qa_agent.server"], cwd=ROOT, env=env)]
    try:
        up("http://127.0.0.1:8601/api/health"); up("http://127.0.0.1:8600/api/config")
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1400, "height": 1000})
            page.goto("http://127.0.0.1:8600/")
            page.add_style_tag(content="body{height:auto!important} #main{height:auto} #chat{overflow:visible!important}")
            page.fill("#msg", PROMPT)
            page.click("#send")
            page.wait_for_selector("#approve", timeout=30000)
            page.screenshot(path=str(SHOTS / "01_plan_awaiting_approval.png"), full_page=True)
            page.click("#approve")
            page.wait_for_selector("#results", timeout=30000)
            page.screenshot(path=str(SHOTS / "02_after_approval_execution_started.png"), full_page=True)
            page.wait_for_selector("#links", timeout=180000)
            page.screenshot(path=str(SHOTS / "03_results_with_evidence.png"), full_page=True)
            rid = page.inner_text("#chat .card b").split()[-1]
            rp = b.new_page(viewport={"width": 1400, "height": 1000})
            rp.goto(f"http://127.0.0.1:8600/api/runs/{rid}/report.html")
            rp.screenshot(path=str(SHOTS / "04_report.png"), full_page=True)
            b.close()
        run = httpx.get(f"http://127.0.0.1:8600/api/runs/{rid}").json()
        print(json.dumps({"run_id": rid, "status": run["status"], "summary": run["summary_json"],
                          "results": {r["id"]: r["status"] for r in run["results"]},
                          "report": str(ROOT / "data/runs" / rid / "report.html")}, indent=2))
    finally:
        for p in procs:
            p.terminate(); p.wait(10)


if __name__ == "__main__":
    main()
