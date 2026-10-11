"""Full local workflow through the HTTP API with REAL Playwright against the synthetic SUT."""
import json, time
import pytest
from fastapi.testclient import TestClient
from qa_agent.server import create_app
from qa_agent.util import sha256

pytestmark = [pytest.mark.integration, pytest.mark.execution, pytest.mark.approval]

PROMPT = "Read the synthetic supplier import story, generate test cases, execute the approved tests, and give me the results."


def wait(c, rid, statuses, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"/api/runs/{rid}").json()
        if r["status"] in statuses:
            return r
        time.sleep(0.5)
    raise AssertionError(f"timeout; last status {r['status']}: {r.get('error')}")


def test_full_workflow_with_seeded_defects(tmp_path, monkeypatch, sut_factory):
    with sut_factory("all") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        c = TestClient(create_app(tmp_path))
        rid = c.post("/api/chat", json={"message": PROMPT}).json()["run_id"]
        r = wait(c, rid, {"awaiting_approval", "failed"})
        assert r["status"] == "awaiting_approval", r["error"]
        assert r["story_key"] == "PCC-DEMO-1" and r["model_label"] == "REPLAYED (mock model)"
        assert [a["id"] for a in r["ac_json"]["acceptance_criteria"]] == ["AC1", "AC2", "AC3", "AC4", "AC5"]
        assert len(r["plan_json"]["test_cases"]) == 8 and r["results"] == []
        # fail-closed: nothing executed against the SUT before approval
        assert not any(a["tool"].startswith(("http.", "browser.")) for a in c.app.state.agent.store.audit_rows(rid))

        assert c.post(f"/api/runs/{rid}/approval", json={"approver": "qa.reviewer", "decision": "approved"}).status_code == 200
        r = wait(c, rid, {"completed", "failed"})
        assert r["status"] == "completed", r["error"]
        st = {x["id"]: x["status"] for x in r["results"]}
        assert st == {"TC-01": "passed", "TC-02": "failed", "TC-03": "passed", "TC-04": "passed",
                      "TC-05": "failed", "TC-06": "failed", "TC-07": "failed", "TC-08": "failed"}, st
        diffs = {x["id"]: x["diffs"] for x in r["results"]}
        assert diffs["TC-02"] == [{"field": "length_mm", "expected": 254.0, "actual": 25.4}]   # inch_factor
        assert diffs["TC-05"][0]["field"] == "attributes.color"                                  # drop_color
        assert diffs["TC-08"] == [{"field": "displayed_count", "expected": 3, "actual": 2}]       # ui_count_off
        # evidence for every failed test
        for x in r["results"]:
            if x["status"] == "failed":
                for k in ("screenshot", "trace", "log"):
                    resp = c.get(f"/api/runs/{rid}/artifacts/{x[k]}")
                    assert resp.status_code == 200 and len(resp.content) > 100, (x["id"], k)
                assert c.get(f"/api/runs/{rid}/artifacts/{x['screenshot']}").content[:8] == b"\x89PNG\r\n\x1a\n"
        # downloadable report with matching hash
        rj = c.get(f"/api/runs/{rid}/report.json")
        assert rj.status_code == 200 and "attachment" in rj.headers["content-disposition"]
        sha = c.get(f"/api/runs/{rid}/artifacts/report.json.sha256").text.split()[0]
        assert sha == sha256(rj.content)
        rep = json.loads(rj.content)
        assert rep["summary"] == {"total": 8, "passed": 3, "failed": 5, "error": 0, "blocked": 0}
        assert rep["per_ac"] == {"AC1": "failed", "AC2": "failed", "AC3": "failed", "AC4": "failed", "AC5": "failed"}
        html = c.get(f"/api/runs/{rid}/report.html").text
        assert "REPLAYED (mock model)" in html and sha in html
        # audit
        audit = [json.loads(l) for l in c.get(f"/api/runs/{rid}/audit.jsonl").text.splitlines()]
        tools = {a["tool"] for a in audit}
        assert {"tracker.get_issue", "model.complete", "env.resolve", "http.request", "browser.navigate",
                "browser.upload_file", "browser.screenshot", "report.generate"} <= tools
        assert all(a["decision"] == "allow" for a in audit) and len(audit) > 40
        # path traversal refused
        assert c.get(f"/api/runs/{rid}/artifacts/..%2F..%2Fqa_agent.db").status_code in (400, 404)

    # persistence across restart: brand-new app on the same data dir
    c2 = TestClient(create_app(tmp_path))
    r2 = c2.get(f"/api/runs/{rid}").json()
    assert r2["status"] == "completed" and len(r2["results"]) == 8 and r2["approval"]["approver"] == "qa.reviewer"
    assert rid in [x["id"] for x in c2.get("/api/runs").json()]
    assert c2.get(f"/api/runs/{rid}/report.html").status_code == 200


def test_all_pass_when_defects_disabled(tmp_path, monkeypatch, sut_factory):
    with sut_factory("") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        c = TestClient(create_app(tmp_path))
        rid = c.post("/api/chat", json={"message": PROMPT}).json()["run_id"]
        wait(c, rid, {"awaiting_approval"})
        c.post(f"/api/runs/{rid}/approval", json={"approver": "qa.reviewer", "decision": "approved"})
        r = wait(c, rid, {"completed", "failed"})
        assert r["summary_json"] == {"total": 8, "passed": 8, "failed": 0, "error": 0, "blocked": 0}, r["results"]


def test_execution_blocked_without_or_after_rejected_approval(tmp_path, monkeypatch, sut_factory):
    with sut_factory("all") as url:
        monkeypatch.setenv("SYNTHETIC_SUT_URL", url)
        app = create_app(tmp_path); c = TestClient(app); agent = app.state.agent
        rid = c.post("/api/chat", json={"message": PROMPT}).json()["run_id"]
        wait(c, rid, {"awaiting_approval"})
        agent.execute(rid, wait=True)   # attempt to bypass the approval step
        r = c.get(f"/api/runs/{rid}").json()
        assert r["status"] == "blocked" and r["results"] == []
        rid2 = c.post("/api/chat", json={"message": PROMPT}).json()["run_id"]
        wait(c, rid2, {"awaiting_approval"})
        c.post(f"/api/runs/{rid2}/approval", json={"approver": "qa.reviewer", "decision": "rejected"})
        assert c.get(f"/api/runs/{rid2}").json()["status"] == "rejected"
        agent.execute(rid2, wait=True)
        assert c.get(f"/api/runs/{rid2}").json()["results"] == []
        assert c.post(f"/api/runs/{rid2}/approval", json={"approver": "x", "decision": "approved"}).status_code == 409
