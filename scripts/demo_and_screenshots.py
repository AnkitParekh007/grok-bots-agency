"""Headless end-to-end demo through the real workspace UI (Playwright drives the chat page like a user).
Starts SUT + agent on loopback, sends the demo prompt, screenshots plan approval and results,
clicks Approve, waits for real execution, prints the summary and report path."""
import os, subprocess, sys, time, json, re
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(os.environ.get("QA_SCREENSHOTS_DIR", ROOT / "docs/screenshots"))
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
    demo_config = json.loads((ROOT / "config/agent.json").read_text(encoding="utf-8"))
    demo_config["model"]["provider"] = "replay"
    demo_config["auth"]["mode"] = "demo"
    config_path = ROOT / "reports/demo/agent.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(demo_config), encoding="utf-8")
    env = dict(os.environ, QA_MODEL_PROVIDER="replay", QA_AUTH_MODE="demo",
               QA_AGENT_CONFIG=str(config_path), QA_AGENT_PORT="8600",
               QA_DATA_DIR=os.environ.get("QA_DATA_DIR", str(ROOT / "reports/demo-data")),
               SUT_DEFECTS=os.environ.get("SUT_DEFECTS", "all"), SYNTHETIC_SUT_URL="http://127.0.0.1:8601")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "sut.app:app", "--host", "127.0.0.1", "--port", "8601"], cwd=ROOT, env=env, creationflags=flags),
             subprocess.Popen([sys.executable, "-m", "qa_agent.server"], cwd=ROOT, env=env, creationflags=flags)]
    try:
        up("http://127.0.0.1:8601/api/health"); up("http://127.0.0.1:8600/api/config")
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1400, "height": 1000})
            page.goto("http://127.0.0.1:8600/")
            expect(page.locator("#model-choice option:checked")).to_contain_text(re.compile("replay", re.I))
            assert page.is_visible("#composer-model")
            assert "replay" in page.locator("#model-choice option:checked").text_content().lower()
            page.fill("#msg", PROMPT)
            with page.expect_response(lambda r: r.url.endswith("/api/chat") and r.request.method == "POST") as submitted:
                page.click("#send")
            rid = submitted.value.json()["run_id"]
            page.wait_for_selector("[data-action=review]", timeout=30000)
            page.locator("#chat [data-action=review]").click()
            page.wait_for_selector("#review-dialog[open]")
            page.screenshot(path=str(SHOTS / "01_plan_awaiting_approval.png"), full_page=True)
            page.click("#approve")
            expect(page.locator("#status")).to_contain_text("Executing", timeout=180000)
            page.screenshot(path=str(SHOTS / "02_after_approval_execution_started.png"), full_page=True)
            expect(page.locator("#status")).to_contain_text("Completed", timeout=300000)
            page.locator("#chat [data-action=evidence]").click()
            page.wait_for_selector("#links", timeout=30000)
            page.screenshot(path=str(SHOTS / "03_results_with_evidence.png"), full_page=True)
            rp = b.new_page(viewport={"width": 1400, "height": 1000})
            rp.goto(f"http://127.0.0.1:8600/api/runs/{rid}/report.html")
            rp.screenshot(path=str(SHOTS / "04_report.png"), full_page=True)
            b.close()
        run = httpx.get(f"http://127.0.0.1:8600/api/runs/{rid}").json()
        print(json.dumps({"run_id": rid, "status": run["status"], "summary": run["summary_json"],
                          "results": {r["id"]: r["status"] for r in run["results"]},
                          "report": str(Path(env["QA_DATA_DIR"]) / "runs" / rid / "report.html")}, indent=2))
    finally:
        for p in procs:
            p.terminate(); p.wait(10)


if __name__ == "__main__":
    main()
