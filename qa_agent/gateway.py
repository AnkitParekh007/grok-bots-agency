"""Tool gateway: default-deny policy check + append-only audit (SQLite + JSONL) for every tool call."""
import json, threading, time
from pathlib import Path
from urllib.parse import urlparse
from . import ROOT
from .util import canon, sha256, now, redact


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
               "args_summary": redact(canon(args))[:300], "decision": decision, "reason": redact(reason)[:500],
               "outcome": redact(outcome)[:300], "duration_ms": dur}
        with self._lock:
            self.store.append_audit(rec)
            rec = {k: rec[k] for k in (*self.store.AUDIT_FIELDS, "prev_hash", "entry_hash", "seq", "payload_hash")}
            line = json.dumps(rec) + "\n"
            with open(self.audit_jsonl, "a") as f:
                f.write(line)
            if run_id:
                with open(self.artifacts.path_for(run_id, "audit.jsonl"), "a") as f:
                    f.write(line)
        return rec

    def record(self, run_id, tool, args, decision, reason, outcome="not_executed"):
        """Audit a decision taken outside invoke() (e.g. model-output validation rejects, budget denials)."""
        return self._audit(run_id, tool, args, decision, reason, outcome, 0)

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
        if rule.get("network") == "model_egress":
            # Loopback (local stub) is allowed; any external model host must use https, be listed in the POLICY file
            # (not the provider config that also sets base_url) AND the provider must be approved.
            u = urlparse(args.get("url", ""))
            host = u.hostname or ""
            from .environments import _is_loopback
            if not _is_loopback(host):
                if rule.get("require_https", True) and u.scheme != "https":
                    return f"model egress to {host} denied: non-loopback model endpoints must use https (got {u.scheme or 'none'})"
                approved_hosts = rule.get("approved_hosts") or []
                if host not in approved_hosts:
                    return f"model egress to {host} not in policy approved_hosts"
                if (ctx or {}).get("provider_approved") is not True:
                    return f"model egress to {host} denied: provider not approved (approved flag + approval_ref required)"
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
