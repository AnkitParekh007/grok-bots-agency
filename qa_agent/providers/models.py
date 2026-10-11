"""Model providers. Model choice is configuration-driven; there is NO automatic fallback between providers.

- ReplayModelProvider: offline, cost-free, reads recordings in fixtures/replay/ (CI default).
- GrokProvider: xAI Chat Completions (OpenAI-compatible) with response_format=json_schema (strict).
  Fails closed on missing enablement/approval/key/pricing; all HTTP goes through the ToolGateway
  ("model.request", default-deny model egress); key read from env at call time, never logged.
"""
import json, os, random, time, uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
import httpx
from ..interfaces import ModelProvider
from ..util import load_json, redact, register_secret_env


class ProviderNotApproved(Exception):
    """Provider not enabled/approved/configured. Never repaired, never falls back."""


class ProviderConfigError(ProviderNotApproved):
    pass


class DataClassDenied(ProviderNotApproved):
    pass


class ModelCallError(Exception):
    """Normalized provider failure. kind in: rate_limited, server_error, timeout, transport, auth, bad_request,
    not_found, invalid_response. Message is redacted; correlation_id ties it to audit rows."""
    def __init__(self, msg, kind="transport", status=None, retryable=False, correlation_id=None, attempts=1):
        super().__init__(msg)
        self.kind, self.status, self.retryable, self.correlation_id, self.attempts = kind, status, retryable, correlation_id, attempts

    def as_dict(self):
        return {"kind": self.kind, "status": self.status, "retryable": self.retryable,
                "correlation_id": self.correlation_id, "attempts": self.attempts, "message": str(self)}


def normalize_status(status):
    if status == 429:
        return "rate_limited", True
    if status in (500, 502, 503, 504):
        return "server_error", True
    if status in (401, 403):
        return "auth", False
    if status == 404:
        return "not_found", False
    return "bad_request", False


class UsageMissing(Exception):
    """Provider returned no usable usage block -> cost unknown -> fail closed."""


@dataclass
class ModelResponse:
    text: str
    provider: str
    model: str
    label: str
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    priced: bool = True
    attempts: int = 1
    correlation_ids: list = field(default_factory=list)


def compose_plan_story(rec: dict) -> dict:
    """Adapter: Milestone A two-step recordings (extract_acceptance_criteria + generate_test_plan) -> plan_story shape."""
    r = rec["responses"]
    if "plan_story" in r:
        return r["plan_story"]
    ac, plan = r["extract_acceptance_criteria"], r["generate_test_plan"]
    cats = rec.get("legacy_categories", {})
    tcs, behaviors = [], []
    for i, tc in enumerate(plan.get("test_cases", []), 1):
        tc = dict(tc)
        bid = f"B-{i}"
        behaviors.append({"id": bid, "ac_refs": list(tc.get("ac_refs", [])), "description": tc.get("title", ""), "testable": True})
        tc.setdefault("category", cats.get(tc.get("id"), "positive"))
        tc.setdefault("behavior_ref", bid)
        tcs.append(tc)
    return {
        "understanding": {"summary": rec.get("legacy_understanding", "Legacy two-step recording (Milestone A)."),
                          "domain": "supplier import / attribute mapping", "assumptions": []},
        "acceptance_criteria": ac.get("acceptance_criteria", []),
        "behaviors": behaviors,
        "selected_skills": sorted({t.get("skill") for t in tcs if isinstance(t.get("skill"), str)}),
        "test_cases": tcs,
        "coverage_gaps": [{"ac_refs": [], "kind": "ambiguous_requirement", "reason": g} for g in ac.get("gaps", [])],
    }


class ReplayModelProvider(ModelProvider):
    name = "replay"
    model = "replay"
    cost_free = True

    def __init__(self, replay_dir: Path, label="REPLAYED (mock model)"):
        self.dir, self.label = Path(replay_dir), label

    def _rec(self, key):
        p = self.dir / f"{key}.json"
        if not p.exists():
            raise KeyError(f"no recording for {key}")
        return load_json(p)

    def complete(self, task, context):
        rec = self._rec(context.get("story_key"))
        if task == "plan_story":
            return compose_plan_story(rec)
        if task not in rec.get("responses", {}):
            raise KeyError(f"no recorded response for task {task}")
        return rec["responses"][task]

    def generate(self, task, context, schema=None, repair_feedback=None, run_id=None):
        # Replay is deterministic: a repair request returns the same recording (repairs cannot "fix" a bad recording).
        return ModelResponse(text=json.dumps(self.complete(task, context)), provider=self.name, model="replay",
                             label=self.label, usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                             cost_usd=0.0, priced=True)

    def estimate_max_cost(self, prompt_chars):
        return 0.0


def _is_loopback(host):
    from ..environments import _is_loopback as f
    return f(host or "")


class GrokProvider(ModelProvider):
    name = "grok"
    cost_free = False
    RETRYABLE = {429, 500, 502, 503, 504}
    PRICE_KEYS = ("input_per_mtok_usd", "cached_input_per_mtok_usd", "output_per_mtok_usd")

    def __init__(self, cfg: dict, gateway=None, transport=None, sleep=time.sleep):
        self.cfg = dict(cfg)
        self.gateway, self.transport, self.sleep = gateway, transport, sleep
        self.model = cfg.get("model")
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        host = urlparse(self.base_url).hostname or ""
        self.is_local_stub = _is_loopback(host)
        self.provider_approved = cfg.get("enabled") is True and cfg.get("approved") is True and bool(cfg.get("approval_ref"))
        if cfg.get("enabled") is not True:
            raise ProviderNotApproved("grok provider not enabled (config providers.grok.enabled=false)")
        if not self.is_local_stub and not self.provider_approved:
            missing = [k for k in ("approved", "approval_ref") if not cfg.get(k)]
            raise ProviderNotApproved(f"grok provider not approved for external endpoint {host} (missing: {missing})")
        if not self.model or not self.base_url:
            raise ProviderConfigError("grok provider requires model and base_url")
        self.key_env = cfg.get("api_key_env", "XAI_API_KEY")
        register_secret_env(self.key_env)
        if not os.environ.get(self.key_env):
            raise ProviderNotApproved(f"grok api key not provided via approved secret reference (env {self.key_env})")
        pricing = cfg.get("pricing") or {}
        if any(not isinstance(pricing.get(k), (int, float)) for k in self.PRICE_KEYS):
            raise ProviderConfigError(f"grok pricing not configured ({', '.join(self.PRICE_KEYS)} per 1M tokens, USD) - refusing unpriced calls")
        self.pricing = pricing
        self.timeout = float(cfg.get("timeout_s", 60))
        self.max_retries = int(cfg.get("max_retries", 3))
        self.backoff_base = float(cfg.get("backoff_base_s", 1.0))
        self.max_backoff = float(cfg.get("max_backoff_s", 30.0))
        self.jitter = float(cfg.get("backoff_jitter_ratio", 0.25))
        self.rng = random.Random()
        self.max_completion_tokens = int(cfg.get("max_completion_tokens", 8000))
        self.allowed_data_classes = list(cfg.get("allowed_data_classes", []))
        self.approved_hosts = list(cfg.get("approved_model_hosts", ["api.x.ai"]))
        self.label = (f"STUB ENDPOINT ({self.model} via local OpenAI-compatible stub at {host}; NOT real model reasoning)"
                      if self.is_local_stub else f"LIVE MODEL ({self.model} via {host})")

    # ---- cost ----
    def cost(self, u):
        p = self.pricing
        uncached = max(u["prompt_tokens"] - u["cached_tokens"], 0)
        return round((uncached * p["input_per_mtok_usd"] + u["cached_tokens"] * p["cached_input_per_mtok_usd"]
                      + u["output_billable_tokens"] * p["output_per_mtok_usd"]) / 1e6, 6)

    def estimate_max_cost(self, prompt_chars):
        # conservative: ~3 chars/token prompt, full output budget
        return round(((prompt_chars / 3) * self.pricing["input_per_mtok_usd"]
                      + self.max_completion_tokens * self.pricing["output_per_mtok_usd"]) / 1e6, 6)

    @staticmethod
    def parse_usage(data):
        u = data.get("usage")
        if not isinstance(u, dict) or not isinstance(u.get("prompt_tokens"), int) or not isinstance(u.get("completion_tokens"), int):
            raise UsageMissing("response has no usable usage block (prompt_tokens/completion_tokens)")
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        reasoning = ((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        total = u.get("total_tokens") or (u["prompt_tokens"] + u["completion_tokens"])
        # xAI examples report total = prompt + completion + reasoning, i.e. reasoning billed in addition to completion.
        out = u["completion_tokens"] + reasoning if total >= u["prompt_tokens"] + u["completion_tokens"] + reasoning and reasoning else u["completion_tokens"]
        return {"prompt_tokens": u["prompt_tokens"], "cached_tokens": cached, "completion_tokens": u["completion_tokens"],
                "reasoning_tokens": reasoning, "total_tokens": total, "output_billable_tokens": out}

    # ---- call ----
    def build_body(self, task, context, schema, repair_feedback=None):
        from ..planning import build_messages
        return {"model": self.model, "messages": build_messages(task, context, repair_feedback),
                "response_format": {"type": "json_schema", "json_schema": {"name": task, "schema": schema, "strict": True}},
                "max_completion_tokens": self.max_completion_tokens}

    def complete(self, task, context):
        raise NotImplementedError("use generate(); callers must validate output against a schema")

    def generate(self, task, context, schema=None, repair_feedback=None, run_id=None):
        if context.get("data_class") not in self.allowed_data_classes:
            raise DataClassDenied(f"data class {context.get('data_class')!r} not approved for provider grok "
                                  f"(allowed: {self.allowed_data_classes})")
        if self.gateway is None:
            raise ProviderNotApproved("grok provider has no tool gateway bound; refusing unaudited egress")
        url = f"{self.base_url}/chat/completions"
        body = self.build_body(task, context, schema, repair_feedback)
        ctx = {"provider_approved": self.provider_approved, "approved_model_hosts": self.approved_hosts}
        cids = []
        for attempt in range(self.max_retries + 1):
            cid = f"{run_id or 'norun'}-{uuid.uuid4().hex[:12]}"
            cids.append(cid)
            args = {"url": url, "model": self.model, "task": task, "attempt": attempt, "correlation_id": cid}
            try:
                resp = self.gateway.invoke(run_id, "model.request", args, lambda: self._post(url, body, cid), ctx)
            except _HTTPStatus as e:
                kind, retryable = normalize_status(e.status)
                if not retryable or attempt == self.max_retries:
                    raise ModelCallError(redact(f"model endpoint returned HTTP {e.status} ({kind}): {e.body[:200]}"),
                                         kind=kind, status=e.status, retryable=retryable, correlation_id=cid, attempts=attempt + 1)
                wait = self._retry_after(e.retry_after)
                self.sleep(wait if wait is not None else self.backoff(attempt))
                continue
            except (httpx.TimeoutException, httpx.TransportError) as e:
                kind = "timeout" if isinstance(e, httpx.TimeoutException) else "transport"
                if attempt == self.max_retries:
                    raise ModelCallError(redact(f"model endpoint unreachable after {attempt + 1} attempts ({kind}): {type(e).__name__}"),
                                         kind=kind, retryable=True, correlation_id=cid, attempts=attempt + 1)
                self.sleep(self.backoff(attempt))
                continue
            try:
                data = resp.json()
            except ValueError:
                raise ModelCallError("model endpoint returned non-JSON HTTP body", kind="invalid_response", status=200,
                                     correlation_id=cid, attempts=attempt + 1)
            usage = self.parse_usage(data)
            choice = (data.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content")) or ""
            return ModelResponse(text=text, provider=self.name, model=data.get("model") or self.model, label=self.label,
                                 finish_reason=choice.get("finish_reason") or "unknown", usage=usage,
                                 cost_usd=self.cost(usage), priced=True, attempts=attempt + 1, correlation_ids=cids)
        raise ModelCallError("exhausted retries", kind="transport", correlation_id=cids[-1] if cids else None)

    def backoff(self, attempt):
        """Exponential backoff with bounded jitter: base*2^attempt * (1 +/- jitter), capped at max_backoff."""
        base = min(self.backoff_base * (2 ** attempt), self.max_backoff)
        return min(base * (1 + self.rng.uniform(-self.jitter, self.jitter)), self.max_backoff)

    def _retry_after(self, v):
        if v is None:
            return None
        try:
            return min(max(float(v), 0.0), self.max_backoff)
        except ValueError:
            return None

    def _post(self, url, body, correlation_id=None):
        key = os.environ.get(self.key_env)
        if not key:
            raise ProviderNotApproved("api key disappeared from environment")
        with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False) as c:
            r = c.post(url, json=body, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                                   "X-Request-ID": correlation_id or ""})
        if r.status_code != 200:
            raise _HTTPStatus(r.status_code, r.headers.get("retry-after"), redact(r.text))
        return r


class _HTTPStatus(Exception):
    def __init__(self, status, retry_after, body):
        super().__init__(f"HTTP {status}")
        self.status, self.retry_after, self.body = status, retry_after, body or ""


def build_model_provider(agent_cfg: dict, root: Path, gateway=None) -> ModelProvider:
    name = os.environ.get("QA_MODEL_PROVIDER") or agent_cfg["model"]["provider"]
    pc = agent_cfg["providers"].get(name)
    if pc is None:
        raise ProviderNotApproved(f"unknown model provider {name!r}")
    if name == "replay":
        return ReplayModelProvider(root / pc["replay_dir"], pc.get("label", "REPLAYED (mock model)"))
    if name == "grok":
        return GrokProvider(pc, gateway=gateway)
    raise ProviderNotApproved(f"provider {name!r} has no implementation")
