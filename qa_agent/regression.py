"""Manual, immutable suites and conservative evidence gates. No execution grants.

Every attempt is admitted before planning. Sealing forbids further attempts; a
passing rerun never replaces an earlier failure. Gates authorize no deployment.
"""
from contextlib import contextmanager
from datetime import datetime
import copy
import json
import math
import re
import time
import uuid

from . import execution_package as EP
from .comparator import compare
from .storage import _regular
from .util import canon, now, sha256


class SuiteDenied(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise SuiteDenied(message)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value), "invalid suite identifier")
    return value


def contract(run):
    """Bind all reviewed inputs, allowing only a different requester for a rerun."""
    require(EP.digest(run) == run.get("plan_hash") == sha256(canon(run.get("execution_envelope_json"))),
            "reviewed execution inputs changed")
    envelope = copy.deepcopy(run["execution_envelope_json"])
    envelope["scope"].pop("requested_by", None)
    return sha256(canon(envelope))


class RegressionSuites:
    def __init__(self, agent, authorization, *, clock=time.time):
        self.agent, self.store, self.authorization, self.clock = agent, agent.store, authorization, clock

    def actor(self, receipt, project, role="requester"):
        require(self.agent.cfg.get("regression", {}).get("enabled") is True, "manual regression suites are disabled")
        p = self.authorization._principal(receipt)
        require(p is not None and p.kind == "human" and p.mode in ("local", "oidc") and
                self.agent.access.allows(p, project, role), "live human project authorization required")
        return p

    @contextmanager
    def transaction(self):
        with self.store.lock:
            require(not self.store.db.in_transaction, "suite operation requires its own transaction")
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.store.db.commit()
            except BaseException:
                self.store.db.rollback()
                raise
        self.agent.gateway.audit_delivery.flush()

    def restored(self, created):
        return bool(self.store.q("SELECT 1 FROM audit WHERE tool='maintenance.restore' AND decision='allow' "
                                 "AND run_id IS NULL AND ts>=? LIMIT 1", (created,)))

    def audit(self, action, value):
        self.agent.gateway.record(None, "regression." + action, value, "allow", "manual suite operation")

    def reviewed(self, run):
        approval = self.agent.approvals.latest(run["id"])
        require(approval and approval.get("decision") == "approved" and approval.get("plan_hash") == run.get("plan_hash") and
                approval.get("auth_mode") in ("local", "oidc") and approval.get("identity") and
                approval["identity"] != run.get("requested_by"), "exact distinct-human plan approval required")
        return approval

    def preview(self, project, suite, version, slots, max_age_s, expires_at, receipt):
        p = self.actor(receipt, project, "approver")
        identifier(suite);identifier(version)
        require(type(max_age_s) is int and 60 <= max_age_s <= 604800, "invalid suite freshness limit")
        require(isinstance(expires_at, str) and len(expires_at) <= 40, "invalid suite expiry")
        try:
            expiry = datetime.fromisoformat(expires_at)
            require(expiry.tzinfo is not None and self.clock() < expiry.timestamp() <= self.clock() + 604800,
                    "suite expiry must be within seven days")
        except (ValueError, OverflowError):
            raise SuiteDenied("invalid suite expiry") from None
        require(isinstance(slots, list) and 1 <= len(slots) <= 20, "suite requires one to twenty slots")
        definition = {"format": "qa-manual-suite/1", "tenant": p.tenant_id, "project": project,
                      "suite": suite, "version": version, "publisher": p.name,
                      "max_age_s": max_age_s, "expires_at": expires_at, "slots": []}
        seen, templates = set(), set()
        for slot in slots:
            require(isinstance(slot, dict) and set(slot) == {"id", "run_id", "plan_hash"}, "invalid suite slot")
            name, run_id = identifier(slot["id"]), identifier(slot["run_id"])
            require(name not in seen and run_id not in templates, "duplicate suite slot or template")
            seen.add(name);templates.add(run_id)
            run = self.store.get_run(run_id)
            require(run and run.get("tenant_id") == p.tenant_id and run.get("project") == project and
                    run.get("requested_by") != p.name and run.get("status") == "completed" and
                    run.get("plan_hash") == slot["plan_hash"] and not self.restored(run["created"]),
                    "reviewed template is unavailable or outside publication scope")
            self.reviewed(run)
            require(run.get("story_key") and run.get("requested_by") and not run.get("cancel_requested"), "invalid suite template")
            self.agent.select(project, run["story_key"], run.get("repository"), run["environment"])
            definition["slots"].append({"id": name, "template_run": run_id, "template_hash": run["plan_hash"],
                "contract": contract(run), "story": run["story_key"], "repository": run.get("repository"),
                "environment": run["environment"], "provider": run["model_provider"], "model_id": run["model_id"]})
        definition["slots"].sort(key=lambda slot: slot["id"])
        require(len(canon(definition).encode()) <= 256000, "suite definition is too large")
        return {"definition": definition, "definition_hash": sha256(canon(definition))}

    def publish(self, project, suite, version, slots, max_age_s, expires_at, expected_hash, receipt):
        with self.transaction():
            preview = self.preview(project, suite, version, slots, max_age_s, expires_at, receipt)
            require(preview["definition_hash"] == expected_hash, "suite definition changed since review")
            d = preview["definition"]
            require(not self.store.q("SELECT 1 FROM regression_versions WHERE tenant=? AND project=? AND suite=? AND version=?",
                                    (d["tenant"], project, suite, version)), "suite version already exists; publish a new version")
            self.store.x("INSERT INTO regression_versions VALUES(?,?,?,?,?,?,?,?)", (d["tenant"], project, suite, version,
                canon(d), expected_hash, canon(receipt), now()))
            self.audit("publish", {"project": project, "suite": suite, "version": version, "hash": expected_hash})
        return preview

    def version(self, project, suite, version, receipt):
        p = self.actor(receipt, project)
        identifier(suite);identifier(version)
        rows = self.store.q("SELECT * FROM regression_versions WHERE tenant=? AND project=? AND suite=? AND version=?",
                            (p.tenant_id, project, suite, version))
        require(rows, "suite version is unavailable")
        row = rows[0]
        d = json.loads(row["definition"])
        require(sha256(canon(d)) == row["digest"] and not self.restored(row["created"]), "suite version requires fresh review")
        publisher = self.actor(json.loads(row["publisher_receipt"]), project, "approver")
        require(publisher.name == d["publisher"], "suite publisher identity changed")
        require(datetime.fromisoformat(d["expires_at"]).timestamp() > self.clock(), "suite version expired")
        return d, row["digest"]

    def open_batch(self, project, suite, version, expected_hash, receipt):
        with self.transaction():
            p = self.actor(receipt, project)
            _, digest = self.version(project, suite, version, receipt)
            require(digest == expected_hash, "wrong suite version hash")
            batch_id = "suite-" + uuid.uuid4().hex
            self.store.x("INSERT INTO regression_batches VALUES(?,?,?,?,?,?,?,?)",
                         (batch_id, p.tenant_id, project, suite, version, digest, p.name, now()))
            self.audit("open", {"batch": batch_id, "hash": digest, "owner": p.name})
        return {"batch_id": batch_id, "suite_hash": digest}

    def batch(self, batch_id, receipt, *, owner=False):
        identifier(batch_id)
        rows = self.store.q("SELECT * FROM regression_batches WHERE id=?", (batch_id,))
        require(rows, "suite batch is unavailable")
        b = rows[0]
        p = self.actor(receipt, b["project"])
        require(b["tenant"] == p.tenant_id and (b["owner"] == p.name or
                (not owner and self.agent.access.allows(p, b["project"], "approver"))), "suite batch is unavailable")
        require(not self.restored(b["created"]), "restored suite batch requires reconciliation and a fresh batch")
        d, digest = self.version(b["project"], b["suite"], b["version"], receipt)
        require(digest == b["suite_digest"], "suite version changed")
        return b, d, p

    def start_attempt(self, batch_id, slot_id, receipt, *, wait=False):
        b, d, p = self.batch(batch_id, receipt, owner=True)
        identifier(slot_id)
        slot = next((s for s in d["slots"] if s["id"] == slot_id), None)
        require(slot is not None, "slot is unavailable")

        def register(run_id):
            # Called inside the run/job creation transaction, before any worker or model.
            current, definition, caller = self.batch(batch_id, receipt, owner=True)
            require(current == b and definition == d and caller == p, "suite admission changed")
            require(not self.store.q("SELECT 1 FROM regression_seals WHERE batch_id=?", (batch_id,)), "suite batch is sealed")
            n = self.store.q("SELECT COUNT(*) AS n FROM regression_attempts WHERE batch_id=? AND slot=?", (batch_id, slot_id))[0]["n"]
            require(n < 3, "slot attempt limit reached")
            self.store.x("INSERT INTO regression_attempts VALUES(?,?,?,?,?)", (run_id, batch_id, slot_id, n + 1, now()))
            self.audit("attempt", {"batch": batch_id, "slot": slot_id, "run": run_id, "ordinal": n + 1})

        return self.agent.start_run("Run reviewed regression story " + slot["story"], slot["environment"], story_key=slot["story"],
            project=b["project"], repository=slot["repository"], model_provider=slot["provider"], model_id=slot["model_id"],
            tenant_id=p.tenant_id, requested_by=p.name, authorization=receipt, wait=wait, _on_create=register)

    def seal(self, batch_id, receipt):
        with self.transaction():
            b, _, p = self.batch(batch_id, receipt, owner=True)
            require(not self.store.q("SELECT 1 FROM regression_seals WHERE batch_id=?", (batch_id,)), "suite batch is already sealed")
            self.store.x("INSERT INTO regression_seals VALUES(?,?,?)", (batch_id, now(), p.name))
            self.audit("seal", {"batch": b["id"], "owner": p.name})
        return {"sealed": True}

    def gate(self, batch_id, receipt):
        """Local evidence only. Bounded work, no source/model/SUT calls or dispatch."""
        b, d, _ = self.batch(batch_id, receipt)
        rows = self.store.q("SELECT * FROM regression_attempts WHERE batch_id=? ORDER BY slot,ordinal LIMIT 61", (batch_id,))
        require(len(rows) <= 60, "suite attempt inventory exceeds its limit")
        sealed = bool(self.store.q("SELECT 1 FROM regression_seals WHERE batch_id=?", (batch_id,)))
        budget = {"bytes": 0, "deadline": time.monotonic() + 10}
        attempts = []
        for row in rows:
            slot = next((s for s in d["slots"] if s["id"] == row["slot"]), None)
            try:
                require(slot is not None, "attempt slot changed")
                result = self.attempt_verdict(row["run_id"], b, d, slot, budget)
            except SuiteDenied as exc:
                result = {"verdict": "UNKNOWN", "reason": str(exc)}
            except (ValueError, KeyError, TypeError, OSError, RuntimeError):
                result = {"verdict": "UNKNOWN", "reason": "reviewed inputs, approval or complete evidence unavailable"}
            attempts.append({"slot": row["slot"], "ordinal": row["ordinal"], "run_id": row["run_id"], **result})
        missing = [s["id"] for s in d["slots"] if not any(a["slot"] == s["id"] for a in attempts)]
        verdict = "FAIL" if any(a["verdict"] == "FAIL" for a in attempts) else (
            "PASS" if sealed and not missing and attempts and all(a["verdict"] == "PASS" for a in attempts) else "UNKNOWN")
        # Recheck live authority and immutable inventory after potentially slow evidence reads.
        self.batch(batch_id, receipt)
        require(rows == self.store.q("SELECT * FROM regression_attempts WHERE batch_id=? ORDER BY slot,ordinal LIMIT 61", (batch_id,)),
                "suite inventory changed during aggregation")
        result = {"format": "qa-manual-suite-gate/1", "batch_id": batch_id, "suite_hash": b["suite_digest"],
                  "scope": {key: b[key] for key in ("tenant", "project", "suite", "version")},
                  "generated_at": now(), "verdict": verdict, "sealed": sealed, "missing_slots": missing,
                  "attempts": attempts, "release_authorized": False}
        result["gate_hash"] = sha256(canon(result))
        self.audit("gate", {"batch": batch_id, "hash": result["gate_hash"], "verdict": verdict})
        return result

    def read(self, run_id, path, budget, limit):
        require(time.monotonic() <= budget["deadline"], "gate evidence deadline exceeded")
        target = self.agent.artifacts.path_for(run_id, path, create=False)
        info = _regular(target)
        require(info.st_size <= limit and budget["bytes"] + info.st_size <= 100000000, "gate evidence byte limit exceeded")
        out = bytearray()
        with target.open("rb") as stream:
            while chunk := stream.read(min(65536, limit + 1 - len(out))):
                out.extend(chunk);budget["bytes"] += len(chunk)
                require(len(out) <= limit and budget["bytes"] <= 100000000 and time.monotonic() <= budget["deadline"],
                        "gate evidence byte/time limit exceeded")
        require(len(out) == info.st_size, "gate evidence changed during reading")
        return bytes(out)

    def attempt_verdict(self, run_id, batch, definition, slot, budget):
        run = self.store.get_run(run_id)
        require(run and run.get("status") == "completed" and not run.get("cancel_requested") and
                run.get("tenant_id") == batch["tenant"] and run.get("project") == batch["project"] and
                run.get("requested_by") == batch["owner"] and run.get("story_key") == slot["story"] and
                run.get("environment") == slot["environment"] and run.get("repository") == slot["repository"] and
                contract(run) == slot["contract"], "run is incomplete or outside suite contract")
        stamp = datetime.fromisoformat(run["updated"])
        require(stamp.tzinfo is not None and 0 <= self.clock() - stamp.timestamp() <= definition["max_age_s"], "run evidence expired")
        approval = self.reviewed(run)
        raw = self.read(run_id, "report.json", budget, 2000000)
        checksum = self.read(run_id, "report.json.sha256", budget, 100)
        require(checksum == (sha256(raw) + "  report.json\n").encode(), "report checksum changed")
        report = json.loads(raw)
        generated = datetime.fromisoformat(report["generated_at"])
        require(generated.tzinfo is not None and 0 <= self.clock() - generated.timestamp() <= definition["max_age_s"],
                "execution report expired")
        results = self.store.results(run_id)
        require(report.get("run_id") == run_id and report.get("plan_hash") == run["plan_hash"] and
                report.get("execution_envelope") == run["execution_envelope_json"] and report.get("results") == results and
                report.get("approval") == approval and report.get("summary") == run.get("summary_json") and
                report.get("coverage_gaps") == run["planning_json"].get("coverage_gaps", []) and
                report.get("acceptance_criteria") == run["ac_json"]["acceptance_criteria"] and
                report.get("source_bundle") == run.get("source_bundle_json") and
                report.get("context_review") == run["planning_json"].get("context_review", []) and
                report.get("run_status_at_report") == "executing", "report differs from reviewed run")
        require(json.loads(self.read(run_id, "inputs/envelope.json", budget, 2000000)) == run["execution_envelope_json"],
                "input envelope snapshot changed")
        oracle_bytes = self.read(run_id, "inputs/oracles.json", budget, 20000000)
        require(sha256(oracle_bytes) == run["execution_envelope_json"]["oracle_sha256"], "oracle snapshot changed")
        oracles = json.loads(oracle_bytes)["oracles"]
        envelope = run["execution_envelope_json"]
        require(sha256(canon(json.loads(self.read(run_id, "inputs/config.json", budget, 2000000)))) == envelope["config_sha256"],
                "configuration snapshot changed")
        require(len(envelope["fixtures"]) <= 100, "fixture inventory exceeds gate limit")
        for name, digest in envelope["fixtures"].items():
            require(sha256(self.read(run_id, "inputs/fixtures/" + name, budget, 20000000)) == digest, "fixture snapshot changed")
        if run.get("source_bundle_json") is not None:
            require(sha256(canon(json.loads(self.read(run_id, "inputs/source-bundle.json", budget, 2000000)))) == envelope["source_bundle_sha256"],
                    "source bundle snapshot changed")
        cases = run["plan_json"].get("test_cases", [])
        require(1 <= len(cases) <= 50 and len(results) == len(cases) and
                {r["id"] for r in results} == {c["id"] for c in cases}, "missing or unexpected case results")
        require(len({c["id"] for c in cases}) == len(cases), "duplicate case ids")
        manifest = report.get("artifacts")
        require(isinstance(manifest, list) and len(manifest) <= 500, "invalid report artifact manifest")
        files = {m["path"].replace("\\", "/"): m for m in manifest}
        require(len(files) == len(manifest), "duplicate artifact paths")
        for result in results:
            require(result.get("evidence_complete") is True and result.get("status") in ("passed", "failed"), "incomplete execution evidence")
            case = next(c for c in cases if c["id"] == result["id"])
            require(result.get("ac_refs") == case["ac_refs"] and result.get("oracle_ref") == case["oracle_ref"],
                    "result acceptance/oracle binding changed")
            oracle = oracles[case["oracle_ref"]]
            tolerance = oracle.get("tolerance", 0.0)
            require(type(tolerance) in (int, float) and math.isfinite(tolerance) and tolerance >= 0 and
                    isinstance(oracle["expected"], dict) and oracle["expected"] and
                    isinstance(result.get("actual"), dict) and result.get("expected") == oracle["expected"],
                    "result expected values differ from the bound oracle")
            compared = compare(oracle["expected"], result["actual"], tolerance)
            require(result["status"] == ("passed" if compared["passed"] else "failed") and
                    result.get("diffs") == compared["diffs"] and result.get("error") in (None, ""),
                    "recorded result differs from independent comparison")
            require(time.monotonic() <= budget["deadline"], "gate comparison deadline exceeded")
            for kind in ("screenshot", "trace", "log"):
                path = result.get(kind)
                require(isinstance(path, str), "required evidence is missing")
                path = path.replace("\\", "/")
                require(path.startswith("evidence/" + result["id"] + "/"), "required evidence is missing")
                item = files[path]
                body = self.read(run_id, path, budget, 20000000)
                require(0 < len(body) == item["bytes"] and sha256(body) == item["sha256"], "evidence artifact changed")
        counts = {s: sum(r["status"] == s for r in results) for s in ("passed", "failed", "error", "blocked")}
        require(run.get("summary_json") == {"total": len(results), **counts}, "result summary changed")
        require(run == self.store.get_run(run_id) and results == self.store.results(run_id) and
                approval == self.agent.approvals.latest(run_id), "run changed during aggregation")
        if any(r["status"] == "failed" for r in results):
            return {"verdict": "FAIL", "reason": "a recorded case failed with complete evidence",
                    "report_hash": sha256(raw), "plan_hash": run["plan_hash"]}
        acs = {a["id"] for a in run["ac_json"]["acceptance_criteria"]}
        covered = {ac for c in cases for ac in c["ac_refs"]}
        require(acs and covered == acs and not run["planning_json"].get("coverage_gaps"), "suite has uncovered acceptance criteria")
        return {"verdict": "PASS",
                "reason": "all recorded cases and required evidence verified", "report_hash": sha256(raw), "plan_hash": run["plan_hash"]}
