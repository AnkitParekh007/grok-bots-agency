"""Read-only administrator registry. Discovery never enables or approves models."""
import copy
import json
from .models import build_model_provider, ProviderNotApproved
from .quota import QuotaManager
from .. import ROOT
from ..util import redact


class ModelRegistry:
    def __init__(self, agent):
        self.agent = agent
        self.quota = QuotaManager(agent.store)
        self.health = {}

    def resolve(self, provider, model=None):
        cfg = copy.deepcopy(self.agent.cfg)
        pc = cfg.get("providers", {}).get(provider)
        if pc is None:
            raise ProviderNotApproved("unknown provider")
        if model and model != pc.get("model"):
            # Explicitly enumerated admin entries, never catalog-controlled switches.
            entry = next((m for m in pc.get("models", []) if m.get("id") == model), None)
            if not entry or entry.get("approved") is not True:
                raise ProviderNotApproved("model not approved by administrator")
            for key in ("id", "version", "structured_output", "context_window", "max_completion_tokens"):
                if key in entry:
                    pc["model" if key == "id" else key] = entry[key]
        cfg["model"]["provider"] = provider
        # An explicit user choice must not be silently overridden by the process default.
        return build_model_provider(cfg, ROOT, gateway=self.agent.gateway, selected=provider)

    def list(self):
        rows = []
        for name, pc in self.agent.cfg["providers"].items():
            models = [{"id": pc.get("model", name), "version": pc.get("version")}] + pc.get("models", [])
            seen = set()
            for model in models:
                mid = model.get("id") or "(not configured)"
                if mid in seen:
                    continue
                seen.add(mid)
                permitted, reason, obj = False, None, None
                try:
                    obj = self.resolve(name, mid if name != "replay" and mid != "(not configured)" else None)
                    # Check independent egress policy even before a connection probe.
                    if name != "replay":
                        reason = self.agent.gateway.check(None, "model.request", {"url": obj.base_url}, {"provider_approved": obj.provider_approved})
                        permitted = reason is None
                    else:
                        permitted = True
                except (ProviderNotApproved, ValueError) as e:
                    reason = redact(str(e))
                h = self.health.get((name, mid), {})
                metrics = self.agent.store.q("SELECT detail FROM telemetry WHERE kind='model_performance' AND provider=? ORDER BY id DESC LIMIT 30", (name,))
                measured = next((json.loads(m["detail"]) for m in metrics if json.loads(m["detail"]).get("model") == mid), {})
                rows.append({"provider": name, "model": mid, "version": h.get("version") or model.get("version"),
                             "classification": pc.get("classification", "offline"), "cost_type": pc.get("cost_type", "local"),
                             "enabled": pc.get("enabled", name == "replay"), "approved": pc.get("approved", name == "replay"),
                             "configured": bool(obj), "permitted": permitted, "reason": reason,
                             "health": h.get("status", "not_checked"), "available_models": h.get("models"),
                             "available": h.get("available"), "memory_bytes": h.get("memory_bytes"),
                             "structured_output": model.get("structured_output", pc.get("structured_output", "recorded")),
                             "context_window": model.get("context_window", pc.get("context_window")),
                             "output_limit": model.get("max_completion_tokens", pc.get("max_completion_tokens")),
                             "latency_ms": measured.get("latency_ms"), "tokens_per_second": measured.get("tokens_per_second"),
                             "quota": self.quota.status(pc.get("quota_scope", name), pc.get("quotas", {})),
                             "last_evaluation": self._last_evaluation(name, mid)})
        return rows

    def _last_evaluation(self, name, mid):
        rows = self.agent.store.q("SELECT detail FROM telemetry WHERE kind='model_evaluation' AND provider=? ORDER BY id DESC LIMIT 30", (name,))
        observed = next((json.loads(r["detail"]) for r in rows if json.loads(r["detail"]).get("model") == mid), None)
        if observed:
            return observed
        directory = ROOT / "reports/eval"
        paths = ([directory / "eval_report.json"] + list(directory.glob("*/eval_report.json")))[:100]
        paths = sorted((p for p in paths if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths:
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                if report.get("provider") == name and report.get("model_id") == mid:
                    return {"model": mid, "verdict": report["verdict"], "score": report.get("aggregate"),
                            "real_reasoning_evidence": report.get("real_reasoning_evidence", False)}
            except (ValueError, OSError, KeyError):
                continue
        return None

    def probe(self, provider, model=None):
        obj = self.resolve(provider, model)
        if provider == "replay":
            return {"status": "offline_recordings", "available": True}
        try:
            models = obj.discover()
            available = any(m["id"] == obj.model for m in models)
            detail = {"status": "connected" if available else "model_unavailable", "available": available, "models": models}
            detail["version"] = obj.version
            if provider == "ollama" and available:
                detail["capabilities"] = obj.inspect_model()
                detail.update(obj.memory())
        except Exception as e:
            detail = {"status": "error", "available": False, "error": redact(str(e))[:300]}
        self.health[(provider, obj.model)] = detail
        self.agent.gateway.record(None, "model.health", {"provider": provider, "model": obj.model}, "allow", detail["status"])
        return detail
