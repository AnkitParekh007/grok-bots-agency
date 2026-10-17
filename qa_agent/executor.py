"""Real Playwright execution (headless Chromium + APIRequestContext). Every target interaction goes
through the ToolGateway (policy + audit). Evidence: screenshot, trace.zip, log per test."""
import json, time, traceback
from pathlib import Path
from playwright.sync_api import sync_playwright
from . import ROOT
from .comparator import compare
from .gateway import PolicyDenied
from .interfaces import TestExecutor
from .util import redact
from .network import EgressProxy
from urllib.parse import urljoin


class ToolContext:
    def __init__(self, run_id, tc_id, page, env, plan_hash, gateway, artifacts, data_dir, fixture_bytes=None):
        self.run_id, self.tc_id, self.page, self.env = run_id, tc_id, page, env
        self.gw, self.art, self.data_dir = gateway, artifacts, Path(data_dir)
        self.ctx = {"env": env, "plan_hash": plan_hash}
        self.lines = []
        self.fixture_bytes = fixture_bytes

    def log(self, msg, obj=None):
        self.lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}" + (f": {json.dumps(obj, default=str)}" if obj is not None else ""))

    def _url(self, path):
        return self.env["base_url"].rstrip("/") + path

    def _file(self, name):
        return str((self.data_dir / name).resolve())

    def _payload(self, name):
        body = self.fixture_bytes[name] if self.fixture_bytes is not None else Path(self._file(name)).read_bytes()
        return {"name": name, "mimeType": "text/csv", "buffer": body}

    def http(self, method, path, file=None):
        url = self._url(path)
        args = {"method": method, "url": url}
        if file:
            args["file"] = self._file(file)

        def do():
            kw = {}
            if file:
                kw["multipart"] = {"file": self._payload(file)}
            current, verb = url, method.upper()
            deadline = time.monotonic() + getattr(self, "api_timeout", 15000) / 1000
            for hop in range(6):
                remaining = max(1, int((deadline - time.monotonic()) * 1000))
                if remaining <= 1:
                    raise TimeoutError("API redirect deadline exceeded")
                r = self.gw.invoke(self.run_id, "execution.network", {"url": current, "method": verb},
                                   lambda: self.page.request.fetch(current, method=verb, timeout=remaining,
                                                                  max_redirects=0, **kw), self.ctx)
                self.log(f"{verb} {path} -> {r.status}")
                if r.status in (301, 302, 303, 307, 308):
                    location = r.headers.get("location")
                    status = r.status
                    r.dispose()
                    if not location or hop == 5:
                        raise RuntimeError("API redirect missing location or exceeds hop limit")
                    current = urljoin(current, location)
                    if status == 303 or (status in (301, 302) and verb == "POST"):
                        verb, kw = "GET", {}
                    continue
                try:
                    if r.status >= 400:
                        raise RuntimeError(f"HTTP {r.status} for {method} {path}")
                    return r.json()
                finally:
                    r.dispose()
        return self.gw.invoke(self.run_id, "http.request", args, do, self.ctx)

    def goto(self, path):
        url = self._url(path)
        self.gw.invoke(self.run_id, "browser.navigate", {"url": url}, lambda: self.page.goto(url), self.ctx)
        self.log(f"navigate {path}")

    def upload(self, selector, name):
        f = self._file(name)
        self.gw.invoke(self.run_id, "browser.upload_file", {"url": self.page.url, "file": f, "selector": selector},
                       lambda: self.page.set_input_files(selector, self._payload(name)), self.ctx)
        self.log(f"upload {name}")

    def screenshot(self):
        rel = f"evidence/{self.tc_id}/screenshot.png"
        p = self.art.path_for(self.run_id, rel, create=False)
        def capture():
            data = self.page.screenshot(full_page=True)
            if not data:
                raise ValueError("screenshot has no evidence bytes")
            return self.art.write_bytes(self.run_id, rel, data)
        self.gw.invoke(self.run_id, "browser.screenshot", {"path": str(p)}, capture, self.ctx)
        return p


class PlaywrightExecutor(TestExecutor):
    def __init__(self, gateway, artifacts, skills, oracles: dict, data_dir, store, timeouts=None, is_cancelled=None, fixture_bytes=None):
        self.gw, self.art, self.skills, self.oracles, self.data_dir, self.store = gateway, artifacts, skills, oracles, data_dir, store
        t = timeouts or {}
        self.action_timeout = int(t.get("action_timeout_ms", 15000))
        self.nav_timeout = int(t.get("navigation_timeout_ms", 15000))
        self.api_timeout = int(t.get("api_timeout_ms", 15000))
        self.is_cancelled = is_cancelled or (lambda rid: False)
        self.fixture_bytes = fixture_bytes

    def run(self, run_id, plan, env, on_event, plan_hash=None):
        results = []
        halted = None
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-quic", "--dns-prefetch-disable",
                                          "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
            try:
                for tc in plan["test_cases"]:
                    if self.is_cancelled(run_id) or halted:
                        res = {"id": tc["id"], "title": tc["title"], "type": tc["type"], "skill": tc["skill"], "ac_refs": tc["ac_refs"],
                               "oracle_ref": tc["oracle_ref"], "expected": self.oracles[tc["oracle_ref"]]["expected"], "actual": None,
                               "diffs": [], "error": halted or "not run: run cancelled", "status": "not_run", "category": tc.get("category")}
                        self.store.save_result(run_id, res)
                        results.append(res)
                        on_event("execute", f"{tc['id']} NOT RUN: {res['error']}")
                        continue
                    on_event("execute", f"{tc['id']} running: {tc['title']}")
                    res = self._run_one(browser, run_id, tc, env, plan_hash)
                    self.store.save_result(run_id, res)
                    results.append(res)
                    if res.get("evidence_complete") is False:
                        halted = "not run: previous test evidence unavailable; target effects require review"
                    on_event("execute", f"{tc['id']} {res['status'].upper()}" +
                             (f" ({len(res['diffs'])} mismatch)" if res.get("diffs") else "") +
                             (f" - {res['error']}" if res.get("error") else ""))
            finally:
                browser.close()
        return results

    def _run_one(self, browser, run_id, tc, env, plan_hash):
        with EgressProxy(self.gw, run_id, env, plan_hash) as proxy:
            return self._run_isolated(browser, run_id, tc, env, plan_hash, proxy)

    def _run_isolated(self, browser, run_id, tc, env, plan_hash, proxy):
        ev = f"evidence/{tc['id']}"
        context = browser.new_context(viewport={"width": 1100, "height": 700}, service_workers="block",
                                      accept_downloads=False, proxy={"server": proxy.url, "bypass": "<-loopback>"})
        def guard(route):
            request = route.request
            reason = self.gw.check(run_id, "execution.network", {"url": request.url, "method": request.method},
                                   {"env": env, "plan_hash": plan_hash})
            if reason:
                try:
                    proxy.deny(request.url, reason)
                except PolicyDenied:
                    route.abort("blockedbyclient")
            else:
                route.continue_()
        context.route("**/*", guard)
        def deny_socket(route):
            try:
                proxy.deny(route.url, "WebSocket execution is not an approved capability")
            except PolicyDenied:
                route.close(code=1008, reason="execution policy")
        context.route_web_socket("**/*", deny_socket)
        context.tracing.start(screenshots=True, snapshots=True)
        context.set_default_timeout(self.action_timeout)
        context.set_default_navigation_timeout(self.nav_timeout)
        page = context.new_page()
        t = ToolContext(run_id, tc["id"], page, env, plan_hash, self.gw, self.art, self.data_dir, self.fixture_bytes)
        t.api_timeout = self.api_timeout
        oracle = self.oracles[tc["oracle_ref"]]
        res = {"id": tc["id"], "title": tc["title"], "type": tc["type"], "skill": tc["skill"], "ac_refs": tc["ac_refs"],
               "oracle_ref": tc["oracle_ref"], "expected": oracle["expected"], "actual": None, "diffs": [], "error": None,
               "category": tc.get("category"), "status": "error"}  # default ERROR: only an explicit comparison can produce "passed"
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
        finally:
            # Evidence is preserved on every outcome, including errors and timeouts.
            res["duration_ms"] = int((time.time() - start) * 1000)
            evidence_errors = []
            try:
                res["screenshot"] = str(t.screenshot().relative_to(self.art.path_for(run_id)))
            except Exception as e:
                t.log(f"screenshot failed: {type(e).__name__}: {e}")
                evidence_errors.append("screenshot")
            try:
                self.art.capture_file(run_id, f"{ev}/trace.zip", lambda path: context.tracing.stop(path=str(path)))
                res["trace"] = f"{ev}/trace.zip"
            except Exception as e:
                t.log(f"trace failed: {e}")
                evidence_errors.append("trace")
            try:
                context.close()
            except Exception as exc:
                t.log(f"browser context cleanup failed: {type(exc).__name__}")
                res["status"], res["error"] = "error", "browser context cleanup failed; target effects require reconciliation"
            if proxy.denials:
                res["status"], res["error"] = "blocked", "execution network policy denied traffic"
                t.log("network policy", {"denials": len(proxy.denials)})
            t.log("result", {k: res.get(k) for k in ("status", "actual", "diffs", "error")})
            try:
                self.art.write_bytes(run_id, f"{ev}/log.txt", redact("\n".join(t.lines)).encode())
                res["log"] = f"{ev}/log.txt"
            except Exception:
                evidence_errors.append("log")
            res["evidence_complete"] = not evidence_errors
            if evidence_errors:
                res["comparison_status"] = res["status"]
                res["status"] = "error"
                res["error"] = "required evidence unavailable: " + ", ".join(evidence_errors) + "; target effects require review"
        return res
