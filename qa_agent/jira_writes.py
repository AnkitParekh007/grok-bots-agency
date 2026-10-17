"""Separate human-reviewed Jira Cloud mutations. Never called by a model/tool plan.

The durable CAS allows one POST attempt. Dispatching/uncertain receipts cannot be
replayed, including after restart. Jira and SQLite have no shared transaction.
"""
from contextlib import contextmanager
import json
import base64
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .util import canon, sha256, register_secret_env, register_secret_value
from .integrations.http import IntegrationError
from .security import RateLimits


class JiraWriteDenied(ValueError):
    pass


class JiraWriteThrottled(JiraWriteDenied):
    def __init__(self, retry_after):
        super().__init__("Jira write admission rate exceeded")
        self.retry_after = retry_after


def _require(ok, message):
    if not ok:
        raise JiraWriteDenied(message)


def _identifier(value, pattern, label):
    _require(isinstance(value, str) and re.fullmatch(pattern, value) is not None, "invalid " + label)
    return value


def _adf(lines):
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in lines]}


class JiraWrites:
    def __init__(self, agent, authorization, *, transport=None, clock=time.time):
        self.agent, self.store, self.authorization = agent, agent.store, authorization
        self.transport, self.clock = transport, clock
        self.rates = RateLimits(self.store, clock=clock)

    def _admit(self, run, principal):
        for scope, key, limit in (("jira_write_actor", principal.name, 10),
                                  ("jira_write_project", canon([run.get("tenant_id"), run["project"]]), 30),
                                  ("jira_write_global", "all", 60)):
            retry = self.rates.consume(scope, key, limit, period=60)
            if retry:
                raise JiraWriteThrottled(retry)

    @contextmanager
    def _transaction(self):
        with self.store.lock:
            _require(not self.store.db.in_transaction, "write action requires its own durable transaction")
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.store.db.commit()
                # In-transaction audit calls intentionally defer projection.
                # Drain after commit so an idle review cannot age the backlog.
                self.agent.gateway.audit_delivery.flush()
            except BaseException:
                self.store.db.rollback()
                raise

    def _run(self, run_id, *, completed=True):
        run = self.store.get_run(run_id)
        _require(run is not None and run.get("project") in self.agent.projects, "write run is unavailable")
        _identifier(run_id, r"[A-Za-z0-9_-]{1,80}", "run id")
        if completed:
            _require(run["status"] == "completed" and not run.get("cancel_requested"), "write requires a completed, non-cancelled run")
            _require(not self._restored(run), "restored run requires reconciliation and a fresh run before Jira writes")
        return run

    def _restored(self, run):
        return bool(self.store.q("SELECT 1 FROM audit WHERE tool='maintenance.restore' AND decision='allow' "
                                 "AND run_id IS NULL AND ts>=? LIMIT 1", (run["created"],)))

    def _actor(self, receipt, run, role):
        p = self.authorization._principal(receipt)
        _require(p is not None and p.kind == "human" and p.mode in ("local", "oidc") and
                 p.tenant_id == run.get("tenant_id", "local") and
                 self.agent.access.allows(p, run["project"], role), "live human project authorization required")
        return p

    def _configuration(self, run):
        cfg = self.agent.cfg.get("jira_writes", {}).get(run["project"], {})
        _require(isinstance(cfg, dict) and cfg.get("enabled") is True and cfg.get("approved") is True and
                 isinstance(cfg.get("approval_ref"), str) and 1 <= len(cfg["approval_ref"]) <= 200,
                 "Jira write capability is disabled or unapproved")
        _require(cfg.get("kind") == "jira_cloud", "only the separate Jira Cloud token write contract is supported")
        base = cfg.get("base_url", "")
        u = urlsplit(base)
        _require(u.scheme == "https" and u.hostname and not u.username and not u.password and not u.query and
                 not u.fragment and u.port in (None, 443), "writes require an approved HTTPS API origin")
        path = u.path.rstrip("/")
        _require(path == "" or (u.hostname == "api.atlassian.com" and
                 re.fullmatch(r"/ex/jira/[a-fA-F0-9-]{36}", path)), "unsupported Jira API base prefix")
        policy = self.agent.policy.get("jira_writes", {})
        _require(self.agent.policy.get("default") == "deny" and
                 u.hostname in policy.get("approved_hosts", []), "Jira write host is not approved in policy")
        project = _identifier(cfg.get("project_key"), r"[A-Z][A-Z0-9_]{1,39}", "Jira project key")
        pc = self.agent.projects[run["project"]]
        _require(pc.get("jira_project") == project and pc.get("tenant_id", "local") == run.get("tenant_id", "local"),
                 "write project/tenant differs from run scope")
        _identifier(run.get("story_key"), re.escape(project) + r"-[1-9][0-9]{0,14}", "source issue key")
        tracker = self.agent.tracker_for(run["project"])
        source = run.get("story_json") or {}
        _require(getattr(tracker, "kind", "") == "jira_cloud" and
                 getattr(tracker, "cfg", {}).get("enabled") is True and
                 getattr(tracker, "cfg", {}).get("approved") is True and
                 bool(getattr(tracker, "cfg", {}).get("approval_ref")) and
                 getattr(tracker, "cfg", {}).get("base_url", "").rstrip("/") == base.rstrip("/") and
                 project in getattr(tracker, "cfg", {}).get("projects", []) and
                 source.get("_source") == "jira_cloud" and source.get("key") == run["story_key"],
                 "write destination requires matching Jira Cloud source provenance")
        _identifier(cfg.get("issue_type_id"), r"[1-9][0-9]{0,19}", "issue type id")
        _require(isinstance(cfg.get("issue_type_name"), str) and 1 <= len(cfg["issue_type_name"]) <= 80,
                 "reviewed issue type name required")
        auth = cfg.get("auth", {})
        _require(auth.get("type") in ("basic", "bearer"), "write credential reference required")
        refs = [auth.get("token_env")] + ([auth.get("user_env")] if auth["type"] == "basic" else [])
        for ref in refs:
            _require(isinstance(ref, str) and ref.isidentifier() and 1 <= len(ref) <= 128,
                     "invalid write credential reference")
            register_secret_env(ref)
            _require(bool(os.environ.get(ref)), "write credential reference is unavailable")
        origin = self.agent.cfg.get("deployment", {}).get("public_url", "")
        w = urlsplit(origin)
        _require(w.scheme == "https" and w.hostname and not w.username and not w.password and
                 w.path in ("", "/") and not w.query and not w.fragment, "protected HTTPS report origin required")
        ttl = cfg.get("review_ttl_s", 600)
        _require(type(ttl) is int and 30 <= ttl <= 900, "invalid write review lifetime")
        return json.loads(canon(cfg)), origin.rstrip("/"), ttl

    def _http(self, cfg, method, path, payload=None):
        """Fixed relative routes only. One network attempt; no redirect/proxy/retry."""
        _require(method in ("GET", "POST") and re.fullmatch(
            r"rest/api/3/(project/[A-Z][A-Z0-9_]{1,39}|issue(?:/[A-Z][A-Z0-9_]{1,39}-[1-9][0-9]{0,14}(?:/(?:comment|transitions))?)?)", path),
            "unsupported Jira write route")
        auth = cfg["auth"]
        token = os.environ[auth["token_env"]]
        if auth["type"] == "basic":
            token = base64.b64encode((os.environ[auth["user_env"]] + ":" + token).encode()).decode()
        headers = {"Authorization": ("Basic " if auth["type"] == "basic" else "Bearer ") + token}
        register_secret_value(headers["Authorization"])
        headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        body = canon(payload).encode() if payload is not None else None
        _require(body is None or len(body) <= 16000, "write payload exceeds limit")
        deadline = time.monotonic() + 15
        with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False, timeout=15) as client:
            params = {"fields": "project,status,updated,issuetype"} if method == "GET" and re.search(r"/issue/[A-Z0-9_]+-[0-9]+$", path) else None
            if method == "GET" and "/project/" in path:
                params = {"expand": "issueTypes"}
            if method == "GET" and path.endswith("/transitions"):
                params = {"expand": "transitions.fields"}
            with client.stream(method, cfg["base_url"].rstrip("/") + "/" + path, headers=headers,
                               params=params, content=body) as response:
                data = bytearray()
                for chunk in response.iter_bytes(16384):
                    data.extend(chunk)
                    _require(len(data) <= 256000 and time.monotonic() <= deadline, "Jira response exceeds limit")
                _require(time.monotonic() <= deadline, "Jira request deadline exceeded")
                # Body/headers/errors may contain source data or secrets: never persist them.
                if response.status_code == 204:
                    return response.status_code, None
                _require(200 <= response.status_code < 300, "Jira request was not confirmed successful")
                try:
                    parsed = json.loads(data)
                except (ValueError, UnicodeError):
                    raise JiraWriteDenied("Jira returned an invalid receipt") from None
                _require(isinstance(parsed, dict), "Jira returned an invalid receipt")
                return response.status_code, parsed

    def _upstream(self, cfg, run, kind, target, transition):
        project = cfg["project_key"]
        if kind == "create":
            status, data = self._http(cfg, "GET", "rest/api/3/project/" + project)
            _require(status == 200 and data.get("key") == project, "Jira project does not match")
            types = data.get("issueTypes")
            _require(isinstance(types, list) and any(t.get("id") == cfg["issue_type_id"] and
                     t.get("name") == cfg["issue_type_name"] and t.get("subtask") is False for t in types if isinstance(t, dict)),
                     "reviewed standard issue type is unavailable")
            return {"project_id": _identifier(data.get("id"), r"[1-9][0-9]{0,19}", "project id")}
        _identifier(target, re.escape(project) + r"-[1-9][0-9]{0,14}", "write target")
        created = self.store.q("SELECT outcome FROM jira_writes WHERE run_id=? AND state='succeeded'", (run["id"],))
        permitted = {run["story_key"]} | {json.loads(r["outcome"]).get("issue_key") for r in created}
        _require(target in permitted, "target is not the source issue or a defect created for this run")
        status, data = self._http(cfg, "GET", "rest/api/3/issue/" + target)
        f = data.get("fields") or {}
        _require(status == 200 and data.get("key") == target and (f.get("project") or {}).get("key") == project,
                 "upstream target differs from approved project")
        snapshot = {"key": target, "id": _identifier(data.get("id"), r"[1-9][0-9]{0,19}", "issue id"),
                    "status_id": (f.get("status") or {}).get("id"), "updated": f.get("updated")}
        _require(isinstance(snapshot["updated"], str) and 1 <= len(snapshot["updated"]) <= 80,
                 "upstream issue has no revision timestamp")
        if kind == "transition":
            mapping = cfg.get("transitions", {}).get(transition)
            _require(isinstance(mapping, dict) and set(mapping) == {"id", "name", "from_status_id", "to_status_id"},
                     "reviewed workflow transition mapping required")
            for key in ("id", "from_status_id", "to_status_id"):
                _identifier(mapping[key], r"[1-9][0-9]{0,19}", "workflow id")
            _require(snapshot["status_id"] == mapping["from_status_id"], "issue is not in reviewed source status")
            status, transitions = self._http(cfg, "GET", "rest/api/3/issue/" + target + "/transitions")
            rows = transitions.get("transitions")
            _require(status == 200 and isinstance(rows, list), "workflow metadata unavailable")
            matches = [t for t in rows if isinstance(t, dict) and t.get("id") == mapping["id"]]
            _require(len(matches) == 1 and matches[0].get("name") == mapping["name"] and
                     (matches[0].get("to") or {}).get("id") == mapping["to_status_id"] and
                     isinstance(matches[0].get("fields"), dict) and
                     all(isinstance(v, dict) and v.get("required") is False for v in matches[0]["fields"].values()),
                     "workflow transition differs or requires unsupported fields")
            snapshot["transition"] = mapping
        return snapshot

    def _evidence(self, run):
        sizes = self.store.q("SELECT LENGTH(CAST(json AS BLOB)) AS bytes FROM results WHERE run_id=? LIMIT 51", (run["id"],))
        _require(0 < len(sizes) <= 50 and all(type(r["bytes"]) is int for r in sizes) and
                 sum(r["bytes"] for r in sizes) <= 2_000_000, "write result evidence exceeds count/byte limit")
        results = self.store.results(run["id"])
        _require(0 < len(results) <= 50, "bounded completed test evidence required")
        failures = []
        for r in results:
            if r.get("status") in ("failed", "error"):
                failures.append({"test_id": _identifier(r.get("id"), r"[A-Za-z0-9_-]{1,80}", "test id"),
                                 "status": r["status"]})
        _require(failures, "no failed or errored test evidence to review")
        artifacts = [a for a in self.agent.artifacts.manifest(run["id"]) if a["path"] != "audit.jsonl"]
        _require(any(a["path"] == "report.json" for a in artifacts), "protected completed report required")
        snapshot = {"run_id": run["id"], "project": run["project"], "tenant": run.get("tenant_id", "local"),
                    "story": run["story_key"], "plan_hash": run.get("plan_hash"),
                    "source_hash": sha256(canon([run.get("story_json"), run.get("source_bundle_json")])),
                    "results_hash": sha256(canon(results)), "artifacts": artifacts}
        return failures, sha256(canon(snapshot))

    def _binding(self, cfg, project):
        from .dependencies import fingerprint
        with Path(__file__).open("rb") as source:
            implementation = source.read(1_000_001)
        _require(len(implementation) <= 1_000_000, "write implementation exceeds review binding limit")
        dependencies = fingerprint()
        _require(dependencies["matches_lock"], "write dependency environment differs from reviewed lock")
        _require(project in self.agent.projects and self.agent.cfg.get("jira_writes", {}).get(project) == cfg,
                 "write capability differs from exact workspace project binding")
        source = {"adapter": self.agent.tracker_for(project).cfg}
        connection = self.agent.projects[project].get("tracker_connection")
        if connection:
            source.update(connection=self.agent.connections._hash(connection), scope=self.agent.connections._scope(connection))
        return sha256(canon({"capability": cfg, "access": self.agent.access.revision,
                             "policy": self.agent.policy.get("jira_writes"),
                             "implementation": sha256(implementation), "dependencies": dependencies, "source": source,
                             "report_origin": self.agent.cfg.get("deployment", {}).get("public_url")}))

    def _audit(self, run_id, op, phase):
        self.agent.gateway.record(run_id, "jira.write." + phase, {"operation_id": op}, "allow",
                                  "human-reviewed write capability", phase)

    def propose(self, run_id, kind, receipt, *, target=None, transition=None):
        _require(kind in ("create", "comment", "transition"), "unsupported write operation")
        _require((kind == "transition") == (transition is not None) and (kind != "create" or target is None),
                 "unexpected write target or transition")
        run = self._run(run_id)
        p = self._actor(receipt, run, "requester")
        cfg, origin, ttl = self._configuration(run)
        self._admit(run, p)
        failures, evidence_hash = self._evidence(run)
        fingerprint = sha256(canon([run_id, kind, target, transition, evidence_hash]))
        old = self.store.q("SELECT id FROM jira_writes WHERE fingerprint=?", (fingerprint,))
        if old:
            return self.get(run_id, old[0]["id"], receipt)
        _require(self.store.q("SELECT COUNT(*) AS n FROM jira_writes WHERE run_id=?", (run_id,))[0]["n"] < 20,
                 "per-run write proposal limit reached")
        upstream = self._upstream(cfg, run, kind, target, transition)
        op = uuid.uuid4().hex
        lines = ["QA Buddy review evidence; failed assertions may need investigation.",
                 "Operation: " + op, "Evidence SHA-256: " + evidence_hash,
                 "Protected report: " + origin + "/api/runs/" + run_id + "/artifacts/report.json"]
        lines += [f"Test {f['test_id']}: {f['status']}" for f in failures]
        if kind == "create":
            path = "rest/api/3/issue"
            payload = {"fields": {"project": {"key": cfg["project_key"]}, "issuetype": {"id": cfg["issue_type_id"]},
                                  "summary": "QA Buddy investigation: " + run["story_key"],
                                  "description": _adf(lines), "labels": ["qa-buddy-" + op]}}
        elif kind == "comment":
            path, payload = "rest/api/3/issue/" + target + "/comment", {"body": _adf(lines)}
        else:
            path = "rest/api/3/issue/" + target + "/transitions"
            payload = {"transition": {"id": upstream["transition"]["id"]}}
        draft = {"id": op, "run_id": run_id, "kind": kind, "target": target, "transition": transition,
                 "destination": cfg["base_url"].rstrip("/") + "/" + path, "path": path, "payload": payload,
                 "configuration_hash": self._binding(cfg, run["project"]), "evidence_hash": evidence_hash,
                 "upstream": upstream, "expires": self.clock() + ttl}
        _require(len(canon(draft).encode()) <= 24000, "write review exceeds limit")
        with self._transaction():
            old = self.store.q("SELECT id FROM jira_writes WHERE fingerprint=?", (fingerprint,))
            if old:
                return self.get(run_id, old[0]["id"], receipt)
            count = self.store.q("SELECT COUNT(*) AS n FROM jira_writes WHERE run_id=?", (run_id,))[0]["n"]
            _require(count < 20, "per-run write proposal limit reached")
            self.store.x("INSERT INTO jira_writes(id,run_id,fingerprint,draft,draft_hash,state,requester,requester_receipt,expires) "
                         "VALUES(?,?,?,?,?,'pending',?,?,?)", (op, run_id, fingerprint, canon(draft), sha256(canon(draft)),
                         p.name, canon(receipt), draft["expires"]))
            self._audit(run_id, op, "proposed")
        return self.get(run_id, op, receipt)

    def _row(self, run_id, op):
        _identifier(op, r"[a-f0-9]{32}", "operation id")
        rows = self.store.q("SELECT * FROM jira_writes WHERE id=? AND run_id=?", (op, run_id))
        _require(bool(rows), "write operation is unavailable")
        return rows[0]

    def get(self, run_id, op, receipt):
        run = self._run(run_id, completed=False)
        self._actor(receipt, run, "requester")
        row = self._row(run_id, op)
        return {"draft": json.loads(row["draft"]), "draft_hash": row["draft_hash"], "state": row["state"],
                "requester": row["requester"], "approver": row["approver"],
                "outcome": json.loads(row["outcome"]) if row["outcome"] else None}

    def list(self, run_id, receipt):
        run = self._run(run_id, completed=False)
        self._actor(receipt, run, "requester")
        available, transitions = False, []
        try:
            cfg, _, _ = self._configuration(run)
            available = run["status"] == "completed" and not run.get("cancel_requested") and not self._restored(run)
            transitions = sorted(k for k in cfg.get("transitions", {}) if isinstance(k, str) and
                                 re.fullmatch(r"[A-Za-z0-9_-]{1,80}", k))
        except (JiraWriteDenied, ValueError, TypeError, KeyError, IntegrationError):
            available = False
        rows = self.store.q("SELECT id FROM jira_writes WHERE run_id=? ORDER BY rowid", (run_id,))
        _require(len(rows) <= 20, "write operation inventory exceeds limit")
        return {"available": bool(available), "transitions": transitions, "source_issue": run.get("story_key"),
                "operations": [self.get(run_id, r["id"], receipt) for r in rows]}

    def review(self, run_id, op, receipt, draft_hash, decision):
        _require(decision in ("approve", "reject"), "invalid write review decision")
        run = self._run(run_id)
        p = self._actor(receipt, run, "approver")
        with self._transaction():
            row = self._row(run_id, op)
            _require(row["state"] == "pending" and self.clock() < row["expires"], "review is stale or already decided")
            _require(p.name not in (row["requester"], run.get("requested_by")), "distinct human write reviewer required")
            _require(draft_hash == row["draft_hash"] == sha256(row["draft"]), "review hash differs from exact draft")
            cfg, _, _ = self._configuration(run)
            self._actor(json.loads(row["requester_receipt"]), run, "requester")
            draft = json.loads(row["draft"])
            _require(draft["configuration_hash"] == self._binding(cfg, run["project"]) and
                     draft["evidence_hash"] == self._evidence(run)[1], "write configuration or evidence changed")
            self.store.x("UPDATE jira_writes SET state=?,approver=?,approver_receipt=? WHERE id=? AND state='pending'",
                         ("approved" if decision == "approve" else "rejected", p.name, canon(receipt), op))
            self._audit(run_id, op, decision)
        return self.get(run_id, op, receipt)

    def dispatch(self, run_id, op, receipt):
        run = self._run(run_id)
        p = self._actor(receipt, run, "approver")
        row = self._row(run_id, op)
        _require(row["state"] == "approved" and p.name == row["approver"], "only the approving human can dispatch once")
        cfg, _, _ = self._configuration(run)
        self._admit(run, p)
        draft = json.loads(row["draft"])
        upstream = self._upstream(cfg, run, draft["kind"], draft["target"], draft["transition"])
        with self._transaction():
            row = self._row(run_id, op)
            run = self._run(run_id)
            cfg, _, _ = self._configuration(run)
            _require(row["state"] == "approved" and self.clock() < row["expires"], "write is stale or already dispatched")
            _require(sha256(row["draft"]) == row["draft_hash"] and draft == json.loads(row["draft"]), "write draft changed")
            self._actor(json.loads(row["requester_receipt"]), run, "requester")
            reviewer = self._actor(json.loads(row["approver_receipt"]), run, "approver")
            _require(reviewer.name == p.name and reviewer.name not in (row["requester"], run.get("requested_by")),
                     "write reviewer no longer authorized")
            _require(draft["configuration_hash"] == self._binding(cfg, run["project"]) and
                     draft["evidence_hash"] == self._evidence(run)[1] and upstream == draft["upstream"],
                     "reviewed configuration, evidence or upstream state changed")
            _require(not self.agent.gateway.audit_delivery.admission_denial(self.agent.policy.get("audit_delivery")),
                     "write audit delivery unavailable")
            self.agent.artifacts.ensure_capacity(run_id)
            self.store.x("UPDATE jira_writes SET state='dispatching',started=? WHERE id=? AND state='approved'", (self.clock(), op))
            self._audit(run_id, op, "intent")
        # The intent is committed before touching Jira. Any later failure is uncertain.
        try:
            self.agent.gateway.audit_delivery.flush()
            _require(not self.agent.gateway.audit_delivery.admission_denial(self.agent.policy.get("audit_delivery")),
                     "write intent delivery unavailable")
            run = self._run(run_id)
            self._actor(json.loads(row["requester_receipt"]), run, "requester")
            self._actor(json.loads(row["approver_receipt"]), run, "approver")
            current, _, _ = self._configuration(run)
            _require(self.clock() < row["expires"] and self._binding(current, run["project"]) == draft["configuration_hash"], "write approval changed")
            _require(self._evidence(run)[1] == draft["evidence_hash"], "write evidence changed after intent")
            self.agent.artifacts.ensure_capacity(run_id)
            status, data = self._http(current, "POST", draft["path"], draft["payload"])
            expected = 204 if draft["kind"] == "transition" else 201
            _require(status == expected, "write success receipt differs from expected contract")
            outcome = {"http_status": status}
            if draft["kind"] == "create":
                outcome["issue_key"] = _identifier(data.get("key"), re.escape(cfg["project_key"]) + r"-[1-9][0-9]{0,14}", "created issue key")
                outcome["issue_id"] = _identifier(data.get("id"), r"[1-9][0-9]{0,19}", "created issue id")
            elif draft["kind"] == "comment":
                outcome["comment_id"] = _identifier(data.get("id"), r"[1-9][0-9]{0,19}", "created comment id")
            state = "succeeded"
        except Exception:
            # Never retain exception/body text or retry even on 429/redirect/auth error.
            state, outcome = "uncertain", {"reconciliation_required": True}
        with self._transaction():
            self.store.x("UPDATE jira_writes SET state=?,outcome=? WHERE id=? AND state='dispatching'", (state, canon(outcome), op))
            self._audit(run_id, op, state)
        return self.get(run_id, op, receipt)
