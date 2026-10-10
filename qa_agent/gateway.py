"""Tool gateway: default-deny policy check + append-only audit (SQLite + JSONL) for every tool call."""
import json, threading, time
from pathlib import Path
from urllib.parse import urlparse
from . import ROOT
from .util import canon, sha256, now


class PolicyDenied(Exception):
    pass


class ToolGateway:
    def __init__(self, policy: dict, store, approvals, envs, artifacts, audit_jsonl: Path):
        self.policy, self.store, self.approvals, self.envs, self.artifacts = policy, store, approvals, envs, artifacts
        self.audit_jsonl = Path(audit_jsonl)
        self.audit_jsonl.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _audit(self, run_id, tool, args, decision, reason, outcome, dur):
        rec = {"ts": now(), "run_id": run_id, "tool": tool, "args_hash": sha256(canon(args)),
               "args_summary": canon(args)[:300], "decision": decision, "reason": reason,
               "outcome": outcome, "duration_ms": dur}
        with self._lock:
            self.store.x("INSERT INTO audit(ts,run_id,tool,args_hash,args_summary,decision,reason,outcome,duration_ms)"
                         " VALUES(:ts,:run_id,:tool,:args_hash,:args_summary,:decision,:reason,:outcome,:duration_ms)", rec)
            line = json.dumps(rec) + "\n"
            with open(self.audit_jsonl, "a") as f:
                f.write(line)
            if run_id:
                with open(self.artifacts.path_for(run_id, "audit.jsonl"), "a") as f:
                    f.write(line)
        return rec

    def check(self, run_id, tool, args, ctx) -> str | None:
        """Return None if allowed, else a denial reason. Fail-closed on anything missing."""
        rule = self.policy.get("tools", {}).get(tool)
        if self.policy.get("default") != "deny":
            return "policy misconfigured: default must be deny"
        if rule is None:
            return f"tool '{tool}' not in allowlist"
        if rule.get("requires_approval"):
            ph = (ctx or {}).get("plan_hash")
            if not self.approvals.is_approved(run_id, ph):
                return "no human approval recorded for this exact plan"
        if rule.get("network") == "environment":
            env = (ctx or {}).get("env")
            url = args.get("url")
            if not env or not url or not self.envs.is_url_allowed(env, url):
                return f"url not allowed for environment: {url}"
        if "methods" in rule and args.get("method", "GET").upper() not in rule["methods"]:
            return f"method {args.get('method')} not allowed"
        if "file_roots" in rule and ("file" in args or tool == "browser.upload_file"):
            f = Path(args.get("file", "")).resolve()
            roots = [(ROOT / r).resolve() for r in rule["file_roots"]]
            if not any(str(f).startswith(str(r) + "/") for r in roots):
                return f"file outside allowed roots: {args.get('file')}"
        return None

    def invoke(self, run_id, tool, args, fn, ctx=None):
        reason = self.check(run_id, tool, args, ctx)
        if reason:
            self._audit(run_id, tool, args, "deny", reason, "not_executed", 0)
            raise PolicyDenied(f"{tool}: {reason}")
        t = time.time()
        try:
            out = fn()
        except Exception as e:
            self._audit(run_id, tool, args, "allow", "policy ok", f"error: {type(e).__name__}: {e}"[:300], int((time.time()-t)*1000))
            raise
        self._audit(run_id, tool, args, "allow", "policy ok", "ok", int((time.time()-t)*1000))
        return out
