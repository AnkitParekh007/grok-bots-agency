"""Explicit source classification and exact model/data-purpose grants.

Operator records are declarations of owner approval, not proof of external legal or
regional controls. Missing, expired, ambiguous or changed decisions fail closed.
"""
import copy
import secrets
import threading
import time
from datetime import datetime

from .providers.models import DataClassDenied
from .util import canon, sha256

CLASSES = {"public", "internal", "confidential", "restricted"}


def _expires(record, clock):
    try:
        value = datetime.fromisoformat(record["expires_at"])
        if value.tzinfo is None or value.timestamp() <= clock():
            raise ValueError()
        return value.timestamp()
    except (KeyError, ValueError, TypeError):
        raise DataClassDenied("data approval requires a future timezone-aware expiry") from None


def _approved(record, clock):
    if not isinstance(record, dict) or record.get("approved") is not True or any(
            not isinstance(record.get(key), str) or not 1 <= len(record[key]) <= 256 or not record[key].strip()
            for key in ("approval_ref", "approved_by")):
        raise DataClassDenied("data-owner approval record is missing")
    return _expires(record, clock)


def _input_hash(task, context):
    from .planning import build_messages
    return sha256(canon(build_messages(task, context)))


class DataPolicy:
    def __init__(self, projects, grants, gateway, *, clock=time.time):
        self.projects, self.grants, self.gateway, self.clock = projects, grants or [], gateway, clock
        if not isinstance(self.grants, list) or len(self.grants) > 1000 or any(not isinstance(g, dict) for g in self.grants):
            raise ValueError("invalid bounded model-data grant list")
        self.lock, self.permits = threading.RLock(), {}
        self.context_collector = None

    def source_rule(self, run):
        project = self.projects.get(run.get("project"), {})
        rule = copy.deepcopy(project.get("source_policy") or {})
        _approved(rule, self.clock)
        if project.get("tenant_id", "local") != run.get("tenant_id", "local") or rule.get("tenant_id") != run.get("tenant_id", "local"):
            raise DataClassDenied("source classification does not match the run tenant/project")
        if rule.get("classification") not in CLASSES:
            raise DataClassDenied("source classification must be explicit; real data cannot be relabeled synthetic")
        fields = rule.get("fields")
        if not isinstance(fields, list) or len(fields) > 30 or any(not isinstance(f, str) for f in fields) or not {"summary", "description"}.issubset(fields):
            raise DataClassDenied("source policy requires an explicit minimized field list")
        return rule

    def prepare(self, run, context, model, *, ac_field=None):
        """Minimize source fields, build a typed corpus, and issue a process-local permit."""
        if context.get("data_class") == "synthetic":
            from .context_sources import selections
            if selections(self.projects.get(run.get("project"), {}).get("source_policy") or {}):
                raise DataClassDenied("selected contextual sources require explicitly classified organization planning")
            return context
        try:
            if model.name == "replay":
                raise DataClassDenied("recorded plans are not organization-model inference")
            rule = self.source_rule(run)
            self._grant(run, "plan_story", rule["classification"], model, rule)
            result = copy.deepcopy(context)
            source = result.get("story") or {}
            fields = source.get("fields") or {}
            result["story"] = {"key": source.get("key"), "fields": {key: fields[key] for key in rule["fields"] if key in fields}}
            summary = result["story"]["fields"].get("summary", "")
            if not isinstance(summary, str) or len(summary.encode()) > 20_000:
                raise DataClassDenied("source summary is unsupported or exceeds input limit")
            result["data_class"] = rule["classification"]
            from .sources import issue_bundle
            custom = ac_field or self.projects[run["project"]].get("acceptance_criteria_field")
            result["source_bundle"] = issue_bundle(result["story"], rule, custom)
            from .context_sources import selections
            selected = selections(rule)
            if selected:
                if self.context_collector is None:
                    raise DataClassDenied("context collection is unavailable")
                sources = self.context_collector(run, rule)
                bundle = result["source_bundle"]
                bundle["sources"] += sources
                bundle["context_ids"] = [source["id"] for source in sources]
                bundle["context_selection_sha256"] = sha256(canon(selected))
                bundle["completeness"] = "approved issue fields and exact selected context only; other sources have not been ingested"
                if len(canon(bundle).encode()) > 300000:
                    raise DataClassDenied("combined source bundle exceeds input limit")
            if canon(rule) != canon(self.source_rule(run)):
                raise DataClassDenied("source approval changed during preparation")
            result["data_permit"] = self.authorize(run, "plan_story", result, model, rule)
            return result
        except DataClassDenied:
            self.gateway.record(run["id"], "model.data_class_gate", {"project": run.get("project"), "provider": model.name},
                                "deny", "source or exact model-data grant missing, expired or incompatible")
            raise

    def _grant(self, run, task, data_class, model, rule):
        if data_class not in getattr(model, "allowed_data_classes", []):
            raise DataClassDenied("provider data-class allowlist does not include the approved classification")
        endpoint = getattr(model, "base_url", "").rstrip("/")
        if not isinstance(self.grants, list) or len(self.grants) > 1000:
            raise DataClassDenied("model-data grant inventory exceeds bounds")
        candidates = [g for g in copy.deepcopy(self.grants) if isinstance(g, dict) and g.get("tenant_id") == run.get("tenant_id", "local") and
                      g.get("project") == run.get("project") and g.get("provider") == model.name and g.get("model") == model.model and
                      g.get("endpoint", "").rstrip("/") == endpoint and g.get("purpose") == task and
                      data_class in g.get("data_classes", [])]
        if len(candidates) != 1:
            raise DataClassDenied("one exact model/source/purpose approval is required")
        grant = candidates[0]
        expires = min(_approved(grant, self.clock), _approved(rule, self.clock))
        declared_region = getattr(model, "cfg", {}).get("region")
        if not declared_region or grant.get("region") != declared_region or any(not isinstance(grant.get(k), str) or not grant[k].strip() for k in ("retention_decision", "training_use_decision")):
            raise DataClassDenied("matching declared region, retention and training-use decisions are required")
        return grant, expires

    def authorize(self, run, task, context, model, rule):
        grant, expires = self._grant(run, task, context.get("data_class"), model, rule)
        permit = secrets.token_urlsafe(32)
        record = {"run_id": run["id"], "tenant_id": run.get("tenant_id", "local"), "project": run.get("project"),
                  "provider": grant["provider"], "model": grant["model"], "endpoint": grant["endpoint"].rstrip("/"), "task": task,
                  "data_class": rule["classification"],
                  "input_hash": _input_hash(task, context), "grant_hash": sha256(canon(grant)), "rule_hash": sha256(canon(rule)),
                  "expires": min(expires, self.clock() + 900)}
        with self.lock:
            self.permits = {key: value for key, value in self.permits.items() if value["expires"] > self.clock()}
            if len(self.permits) >= 1000:
                raise DataClassDenied("model-data authorization capacity exceeded")
            self.permits[permit] = record
        self.gateway.record(run["id"], "model.data_class_gate", {k: record[k] for k in ("project", "provider", "model", "input_hash", "grant_hash", "rule_hash")},
                            "allow", "explicit source classification and exact model-data approval")
        return permit

    def verify(self, run_id, task, context, model):
        if context.get("data_class") == "synthetic":
            if (context.get("source_bundle") or {}).get("classification") in CLASSES:
                raise DataClassDenied("organization source content cannot become synthetic by changing a label")
            return
        permit = context.get("data_permit")
        with self.lock:
            record = self.permits.get(permit) if isinstance(permit, str) else None
        if not record or record["expires"] <= self.clock() or any(record[k] != v for k, v in {
                "run_id": run_id, "task": task, "provider": model.name, "model": model.model,
                "data_class": context.get("data_class"),
                "endpoint": model.base_url.rstrip("/"), "input_hash": _input_hash(task, context)}.items()):
            raise DataClassDenied("model-data permit is absent, expired or bound to different input")
        run = self.gateway.store.get_run(run_id)
        if (context.get("source_bundle") or {}).get("classification") != context.get("data_class"):
            raise DataClassDenied("source and model-input classification do not match")
        rule = self.source_rule(run) if run else None
        if not rule or sha256(canon(rule)) != record["rule_hash"] or not any(sha256(canon(g)) == record["grant_hash"] for g in self.grants):
            raise DataClassDenied("model-data approval changed; re-plan required")
        if run.get("tenant_id", "local") != record["tenant_id"] or run.get("project") != record["project"]:
            raise DataClassDenied("model-data permit tenant/project changed")
