"""Real Playwright execution (headless Chromium + APIRequestContext). Every target interaction goes
through the ToolGateway (policy + audit). Evidence: screenshot, trace.zip, log per test."""
import json, time, traceback
from pathlib import Path
from playwright.sync_api import sync_playwright
from . import ROOT
from .comparator import compare
from .gateway import PolicyDenied
from .interfaces import TestExecutor


class ToolContext:
    def __init__(self, run_id, tc_id, page, env, plan_hash, gateway, artifacts, data_dir):
        self.run_id, self.tc_id, self.page, self.env = run_id, tc_id, page, env
        self.gw, self.art, self.data_dir = gateway, artifacts, Path(data_dir)
        self.ctx = {"env": env, "plan_hash": plan_hash}
        self.lines = []

    def log(self, msg, obj=None):
        self.lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}" + (f": {json.dumps(obj, default=str)}" if obj is not None else ""))

    def _url(self, path):
        return self.env["base_url"].rstrip("/") + path

    def _file(self, name):
        return str((self.data_dir / name).resolve())

    def http(self, method, path, file=None):
        url = self._url(path)
        args = {"method": method, "url": url}
        if file:
            args["file"] = self._file(file)

        def do():
            kw = {}
            if file:
                kw["multipart"] = {"file": {"name": file, "mimeType": "text/csv", "buffer": Path(args["file"]).read_bytes()}}
            r = self.page.request.fetch(url, method=method, **kw)
            self.log(f"{method} {path} -> {r.status}")
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status} for {method} {path}")
            return r.json()
        return self.gw.invoke(self.run_id, "http.request", args, do, self.ctx)

    def goto(self, path):
        url = self._url(path)
        self.gw.invoke(self.run_id, "browser.navigate", {"url": url}, lambda: self.page.goto(url), self.ctx)
        self.log(f"navigate {path}")

    def upload(self, selector, name):
        f = self._file(name)
        self.gw.invoke(self.run_id, "browser.upload_file", {"url": self.page.url, "file": f, "selector": selector},
                       lambda: self.page.set_input_files(selector, f), self.ctx)
        self.log(f"upload {name}")

    def screenshot(self):
        p = self.art.path_for(self.run_id, f"evidence/{self.tc_id}/screenshot.png")
        self.gw.invoke(self.run_id, "browser.screenshot", {"path": str(p)},
                       lambda: self.page.screenshot(path=str(p), full_page=True), self.ctx)
        return p


class PlaywrightExecutor(TestExecutor):
    def __init__(self, gateway, artifacts, skills, oracles: dict, data_dir, store):
        self.gw, self.art, self.skills, self.oracles, self.data_dir, self.store = gateway, artifacts, skills, oracles, data_dir, store

    def run(self, run_id, plan, env, on_event, plan_hash=None):
        results = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for tc in plan["test_cases"]:
                    on_event("execute", f"{tc['id']} running: {tc['title']}")
                    res = self._run_one(browser, run_id, tc, env, plan_hash)
                    self.store.save_result(run_id, res)
                    results.append(res)
                    on_event("execute", f"{tc['id']} {res['status'].upper()}" +
                             (f" ({len(res['diffs'])} mismatch)" if res.get("diffs") else "") +
                             (f" - {res['error']}" if res.get("error") else ""))
            finally:
                browser.close()
        return results

    def _run_one(self, browser, run_id, tc, env, plan_hash):
        ev = f"evidence/{tc['id']}"
        context = browser.new_context(viewport={"width": 1100, "height": 700})
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()
        t = ToolContext(run_id, tc["id"], page, env, plan_hash, self.gw, self.art, self.data_dir)
        oracle = self.oracles[tc["oracle_ref"]]
        res = {"id": tc["id"], "title": tc["title"], "type": tc["type"], "skill": tc["skill"], "ac_refs": tc["ac_refs"],
               "oracle_ref": tc["oracle_ref"], "expected": oracle["expected"], "actual": None, "diffs": [], "error": None}
        start = time.time()
        try:
            _, fn = self.skills.get(tc["skill"]).actions[tc["action"]]
            res["actual"] = fn(t, tc["params"])
            cmp = compare(oracle["expected"], res["actual"], oracle.get("tolerance", 0.0))
            res["diffs"] = cmp["diffs"]
            res["status"] = "passed" if cmp["passed"] else "failed"
        except PolicyDenied as e:
            res["status"], res["error"] = "blocked", str(e)
        except Exception as e:
            res["status"], res["error"] = "error", f"{type(e).__name__}: {e}"
            t.log("exception", traceback.format_exc())
        res["duration_ms"] = int((time.time() - start) * 1000)
        try:
            res["screenshot"] = str(t.screenshot().relative_to(self.art.path_for(run_id)))
        except Exception as e:
            t.log(f"screenshot failed: {e}")
        trace = self.art.path_for(run_id, f"{ev}/trace.zip")
        context.tracing.stop(path=str(trace))
        context.close()
        res["trace"] = f"{ev}/trace.zip"
        t.log("result", {k: res[k] for k in ("status", "actual", "diffs", "error")})
        self.art.write_bytes(run_id, f"{ev}/log.txt", "\n".join(t.lines).encode())
        res["log"] = f"{ev}/log.txt"
        return res
