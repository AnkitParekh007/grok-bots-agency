"""Model providers. Model choice is configuration-driven; there is NO automatic fallback between providers.

- ReplayModelProvider: offline, cost-free, reads recordings in fixtures/replay/ (CI default).
- OpenAICompatibleProvider: provider Chat Completions (OpenAI-compatible) with response_format=json_schema (strict).
  Fails closed on missing enablement/approval/key/pricing; all HTTP goes through the ToolGateway
  ("model.request", default-deny model egress); key read from env at call time, never logged.
"""
import json, os, random, time, uuid, math
from .quota import QuotaManager, QuotaExceeded, retry_seconds
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
import httpx
from ..interfaces import ModelProvider
from ..gateway import PolicyDenied
from ..util import load_json, redact, register_secret_env, register_secret_value


class ProviderNotApproved(Exception):
    """Provider not enabled/approved/configured. Never repaired, never falls back."""


class ProviderConfigError(ProviderNotApproved):
    pass


class DataClassDenied(ProviderNotApproved):
    pass


class BudgetExceeded(Exception):
    """A per-run or daily spend cap would be exceeded by reserving the next request's worst-case cost."""


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
    version: str | None = None
    metrics: dict = field(default_factory=dict)


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


class OpenAICompatibleProvider(ModelProvider):
    name = "compatible"
    cost_free = False
    RETRYABLE = {429, 500, 502, 503, 504}
    PRICE_KEYS = ("input_per_mtok_usd", "cached_input_per_mtok_usd", "output_per_mtok_usd")

    per_attempt_budget = True   # runtime passes a reservation ledger; every HTTP attempt is reserved + settled

    def __init__(self, cfg: dict, gateway=None, transport=None, sleep=time.sleep):
        import copy
        self.cfg = copy.deepcopy(cfg)
        self.name = cfg.get("provider_id", "compatible")
        self.gateway, self.transport, self.sleep = gateway, transport, sleep
        self.model = cfg.get("model")
        self._configured_model = self.model
        self.version = cfg.get("version")
        self.metrics = {}
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self._configured_url = self.base_url
        u = urlparse(self.base_url)
        host = u.hostname or ""
        self.is_local_stub = self.name == "compatible" and _is_loopback(host)
        self.classification = cfg.get("classification", "hosted")
        self.cost_type = cfg.get("cost_type", "paid")
        if self.name in ("groq", "gemini", "openrouter") and (self.classification != "hosted" or self.cost_type != "free"):
            raise ProviderConfigError("unexpected billing mode or classification for free hosted adapter")
        if self.name in ("ollama", "lmstudio", "llamacpp", "vllm") and self.classification != "local":
            raise ProviderConfigError("local engine requires local classification")
        self.cost_free = self.cost_type in ("local", "free")
        self.per_attempt_budget = not self.cost_free
        self.provider_approved = cfg.get("enabled") is True and cfg.get("approved") is True and bool(cfg.get("approval_ref"))
        if cfg.get("enabled") is not True:
            raise ProviderNotApproved(f"{self.name} provider not enabled")
        if (self.name != "compatible" or not self.is_local_stub) and not self.provider_approved:
            raise ProviderNotApproved(f"{self.name} provider not approved for external endpoint {host} (approved and approval_ref required)")
        if not self.model or not isinstance(self.model, str) or not self.base_url:
            raise ProviderConfigError(f"{self.name} provider requires model and base_url")
        if u.scheme not in ("http", "https") or u.username or u.password or u.query or u.fragment:
            raise ProviderConfigError("endpoint must be http(s) without credentials, query or fragment")
        if not _is_loopback(host) and u.scheme != "https":
            raise ProviderConfigError(f"{self.name} base_url must use https for non-loopback host {host}")
        if self.classification == "local" and not _is_loopback(host) and cfg.get("remote_local_approved") is not True:
            raise ProviderNotApproved("remote local endpoint requires explicit LAN authorization and TLS")
        if self.classification not in ("local", "hosted") or self.cost_type not in ("local", "free", "paid"):
            raise ProviderConfigError("invalid classification or cost type")
        if self.classification == "local" and self.cost_type != "local":
            raise ProviderConfigError("local engine must have local cost type")
        if self.classification == "hosted" and self.cost_type == "local" and not self.is_local_stub:
            raise ProviderConfigError("unexpected billing mode: hosted provider cannot have local cost type")
        self.key_env = cfg.get("api_key_env", "MODEL_API_KEY")
        if self.classification == "local" and not _is_loopback(host) and (not self.key_env or not os.environ.get(self.key_env)):
            raise ProviderNotApproved("remote local endpoint requires an authenticated TLS gateway credential")
        register_secret_env(self.key_env)
        if self.classification == "hosted" and not self.is_local_stub and os.environ.get("CI") and self.transport is None:
            raise ProviderNotApproved("hosted providers disabled in CI")
        if self.classification == "hosted" and (not self.key_env or not os.environ.get(self.key_env)):
            raise ProviderNotApproved(f"{self.name} api key not provided via approved secret reference (env {self.key_env})")
        if cfg.get("free_only") is True and self.cost_type != "free":
            raise ProviderNotApproved("unexpected billing mode: free-only provider cannot become billable")
        if self.cost_type == "free" and (cfg.get("free_only") is not True or cfg.get("account_tier") != "free" or not cfg.get("account_verified_ref")):
            raise ProviderNotApproved("unexpected billing mode: free-only account verification required")
        if self.name == "openrouter" and (not self.model.endswith(":free") or self.model == "openrouter/free"):
            raise ProviderNotApproved("OpenRouter requires an explicitly selected :free model ID")
        if self.name == "gemini" and "flash" not in self.model.lower():
            raise ProviderConfigError("Gemini free evaluation requires an account-verified Flash model")
        if self.name == "nvidia":
            raise ProviderNotApproved("NVIDIA NIM deferred until developer access, permitted use and free availability are confirmed")
        pricing = cfg.get("pricing") or {}
        if not self.cost_free and (any(not isinstance(pricing.get(k), (int, float)) or isinstance(pricing.get(k), bool)
                or not math.isfinite(pricing.get(k)) or pricing.get(k) < 0 for k in self.PRICE_KEYS)
                or not pricing.get("input_per_mtok_usd") or not pricing.get("output_per_mtok_usd")):
            raise ProviderConfigError("provider pricing not configured - refusing unpriced calls")
        self.pricing = pricing if not self.cost_free else {k: 0 for k in self.PRICE_KEYS}
        self.timeout = float(cfg.get("timeout_s", 60))
        self.max_retries = int(cfg.get("max_retries", 3))
        self.backoff_base = float(cfg.get("backoff_base_s", 1.0))
        self.max_backoff = float(cfg.get("max_backoff_s", 30.0))
        self.jitter = float(cfg.get("backoff_jitter_ratio", 0.25))
        self.rng = random.Random()
        self.max_completion_tokens = int(cfg.get("max_completion_tokens", 8000))
        self.context_window = int(cfg.get("context_window", 131072))
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 600 or not 0 <= self.max_retries <= 5:
            raise ProviderConfigError("timeout/retries exceed bounded limits")
        if any(not math.isfinite(v) or v < 0 for v in (self.backoff_base, self.max_backoff, self.jitter)) or self.max_backoff > 120:
            raise ProviderConfigError("invalid bounded backoff configuration")
        if not 0 < self.max_completion_tokens < self.context_window:
            raise ProviderConfigError("model compatibility error: output limit must fit context window")
        self.structured_output = cfg.get("structured_output", "json_schema")
        if self.structured_output not in ("json_schema", "json_object"):
            raise ProviderConfigError("model compatibility error: structured JSON output required")
        self.allowed_data_classes = list(cfg.get("allowed_data_classes", []))
        self.quota_limits = cfg.get("quotas", {})
        try:
            QuotaManager.validate(self.quota_limits)
        except ValueError as e:
            raise ProviderConfigError(str(e))
        if self.cost_type == "free" and not all(k in self.quota_limits for k in ("rpm", "rpd", "tpm", "tpd", "concurrency")):
            raise ProviderConfigError("free hosted provider requires explicit request/token/concurrency quotas")
        self.quota_scope = cfg.get("quota_scope", self.name)
        self.quota = QuotaManager(gateway.store) if gateway and self.quota_limits else None
        self.label = (f"STUB ENDPOINT ({self.model} via local OpenAI-compatible stub at {host}; NOT real model reasoning)"
                      if self.is_local_stub else f"LIVE MODEL ({self.name}/{self.model} via {host})")

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
        if not isinstance(u, dict):
            raise UsageMissing("response has no usable usage block")
        def count(value):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise UsageMissing("response usage has invalid token counts")
            return value
        prompt, completion = count(u.get("prompt_tokens")), count(u.get("completion_tokens"))
        details_p, details_c = u.get("prompt_tokens_details") or {}, u.get("completion_tokens_details") or {}
        if not isinstance(details_p, dict) or not isinstance(details_c, dict):
            raise UsageMissing("response usage details are malformed")
        cached = count(details_p.get("cached_tokens", 0))
        reasoning = count(details_c.get("reasoning_tokens", 0))
        total = count(u.get("total_tokens") if u.get("total_tokens") is not None else prompt+completion)
        if cached > prompt or total < prompt+completion:
            raise UsageMissing("response usage is inconsistent")
        # Preserve conservative extra reasoning only when the total reports it separately.
        out = completion+reasoning if reasoning and total >= prompt+completion+reasoning else completion
        return {"prompt_tokens": prompt, "cached_tokens": cached, "completion_tokens": completion,
                "reasoning_tokens": reasoning, "total_tokens": total, "output_billable_tokens": out}

    # ---- call ----
    def build_body(self, task, context, schema, repair_feedback=None):
        from ..planning import build_messages
        if self.name == "groq":
            schema = self.strict_schema(schema, context)
        body = {"model": self.model, "messages": build_messages(task, context, repair_feedback),
                "response_format": {"type": "json_schema", "json_schema": {"name": task, "schema": schema, "strict": True}},
                "max_completion_tokens": self.max_completion_tokens}
        if self.structured_output == "json_object":
            body["response_format"] = {"type": "json_object"}
            body["messages"][0]["content"] += "\nReturn JSON matching this schema: " + json.dumps(schema)
        if self.name == "llamacpp" and self.structured_output == "json_schema":
            body["response_format"] = {"type": "json_object", "schema": schema}
        if self.name in ("lmstudio", "llamacpp", "vllm"):
            body["max_tokens"] = body.pop("max_completion_tokens")
        if self.name == "openrouter":
            body["provider"] = {"allow_fallbacks": False, "require_parameters": True}
        return body

    @staticmethod
    def strict_schema(schema, context):
        """Close dynamic action params for strict hosted schemas; keep runtime validation unchanged."""
        import copy
        schema = copy.deepcopy(schema)
        try:
            params = schema["properties"]["test_cases"]["items"]["properties"]["params"]
        except (KeyError, TypeError):
            return schema
        variants = []
        seen = set()
        for skill in (context.get("catalog") or {}).get("skill_catalog", []):
            for action in skill.get("actions", []):
                keys = tuple(sorted(action.get("params", {})))
                if keys in seen: continue
                seen.add(keys)
                props = {k: {"type": "string", "maxLength": 200} for k in keys}
                variants.append({"type": "object", "properties": props, "required": list(keys), "additionalProperties": False})
        if variants:
            schema["properties"]["test_cases"]["items"]["properties"]["params"] = {"anyOf": variants}
        else:
            raise ProviderConfigError("model compatibility error: strict QA planning requires a parameter catalog")
        def types(obj):
            if isinstance(obj, dict):
                if "enum" in obj and "type" not in obj: obj["type"] = "string"
                for v in obj.values(): types(v)
            elif isinstance(obj, list):
                for v in obj: types(v)
        types(schema)
        return schema

    def complete(self, task, context):
        raise NotImplementedError("use generate(); callers must validate output against a schema")

    def generate(self, task, context, schema=None, repair_feedback=None, run_id=None, budget=None):
        if context.get("data_class") not in self.allowed_data_classes or context.get("data_class") == "unclassified":
            raise DataClassDenied(f"data class {context.get('data_class')!r} not approved for provider {self.name}")
        if self.gateway is None:
            raise ProviderNotApproved("provider has no tool gateway bound; refusing unaudited egress")
        def data_gate():
            policy = getattr(self.gateway, "data_policy", None)
            try:
                if context.get("data_class") != "synthetic" and policy is None:
                    raise DataClassDenied("non-synthetic input requires explicit source and model-data approval")
                if policy:
                    policy.verify(run_id, task, context, self)
            except DataClassDenied:
                self.gateway.record(run_id, "model.data_class_gate", {"provider": self.name, "task": task},
                                    "deny", "model-data permit validation failed")
                raise
        data_gate()
        if self.name != "compatible" and self.model != self._configured_model:
            raise ProviderNotApproved("selected model changed after administrator configuration")
        if self.name != "compatible" and self.base_url != self._configured_url:
            raise ProviderNotApproved("endpoint changed after administrator configuration")
        if self.classification == "hosted" and not self.is_local_stub and os.environ.get("CI") and self.transport is None:
            raise ProviderNotApproved("hosted providers disabled in CI")
        url = self.base_url + self.completion_path()
        body = self.build_body(task, context, schema, repair_feedback)
        # UTF-8 byte count is a conservative token ceiling; no tokenizer download required.
        prompt_tokens = len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + 256
        if prompt_tokens + self.max_completion_tokens > self.context_window:
            raise ProviderConfigError("model compatibility error: request exceeds configured context window")
        ctx = {"provider_approved": self.provider_approved}
        cids, started = [], time.monotonic()
        for attempt in range(self.max_retries + 1):
            data_gate()
            cid = f"{run_id or 'norun'}-{uuid.uuid4().hex[:12]}"
            cids.append(cid)
            args = {"url": url, "provider": self.name, "model": self.model, "version": self.version,
                    "task": task, "attempt": attempt, "correlation_id": cid}
            est = self.estimate_max_cost(len(json.dumps(body)))
            qid, h = None, None
            try:
                qid = self.quota.reserve(self.quota_scope, prompt_tokens+self.max_completion_tokens, self.quota_limits, self.timeout) if self.quota else None
                h = budget.reserve(attempt, est) if budget else None
            except Exception:
                if self.quota: self.quota.settle(qid, outcome="denied_not_sent")
                self.gateway.record(run_id, "model.quota_gate", args, "deny", "reservation refused")
                raise
            settle = (lambda u, c, pr, out: budget.settle(h, u, c, pr, out)) if budget else (lambda *a: None)
            def qsettle(usage=None, outcome="unknown"):
                if self.quota: self.quota.settle(qid, (usage or {}).get("total_tokens"), outcome)
            try:
                resp = self.gateway.invoke(run_id, "model.request", args, lambda: self._post(url, body, cid), ctx)
            except (PolicyDenied, ProviderNotApproved):
                settle(None, 0.0, False, "denied_not_sent"); qsettle(outcome="denied_not_sent")
                raise
            except _HTTPStatus as e:
                settle(None, est, False, f"http_{e.status}"); qsettle(outcome=f"http_{e.status}")
                if self.quota: self.quota.observe(self.quota_scope, e.headers)
                kind, retryable = normalize_status(e.status)
                delay = retry_seconds(e.retry_after)
                if not retryable or attempt == self.max_retries or (delay is not None and delay > self.max_backoff):
                    raise ModelCallError(redact(f"model endpoint returned HTTP {e.status} ({kind}): {e.body[:200]}"),
                                         kind=kind, status=e.status, retryable=retryable, correlation_id=cid, attempts=attempt+1)
                self.sleep(delay if delay is not None else self.backoff(attempt))
                continue
            except (httpx.TimeoutException, httpx.TransportError) as e:
                kind = "timeout" if isinstance(e, httpx.TimeoutException) else "transport"
                settle(None, est, False, kind); qsettle(outcome=kind)
                if attempt == self.max_retries:
                    raise ModelCallError(f"model endpoint unreachable after {attempt+1} attempts ({kind}): {type(e).__name__}",
                                         kind=kind, retryable=True, correlation_id=cid, attempts=attempt+1)
                self.sleep(self.backoff(attempt)); continue
            try:
                raw = resp.json()
                if not isinstance(raw, dict):
                    raise ValueError("expected response object")
                data = self.decode(raw)
                returned = data.get("model")
                if self.name != "compatible" and returned and returned != self.model:
                    raise ProviderNotApproved("response changed the selected model; refusing provider/model switching")
                if self.cost_type == "free" and (raw.get("billing_mode", "free") != "free" or (raw.get("usage") or {}).get("cost", 0) not in (0, None)):
                    raise ProviderNotApproved("unexpected billing mode in free-only response")
                usage = self.parse_usage(data)
                version = data.get("model_version") or self.version or raw.get("system_fingerprint")
                if version is not None and (not isinstance(version, str) or len(version) > 500):
                    raise ValueError("invalid model version metadata")
                choice = (data.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                if not isinstance(text, str): raise ValueError("content is not text")
            except UsageMissing:
                settle(None, est, False, "usage_missing"); qsettle(outcome="usage_missing")
                raise
            except Exception as e:
                settle(None, est, False, "invalid_response"); qsettle(outcome="invalid_response")
                if isinstance(e, (ModelCallError, ProviderNotApproved)): raise
                raise ModelCallError("model endpoint returned non-JSON HTTP body or malformed response", kind="invalid_response", status=200,
                                     correlation_id=cid, attempts=attempt+1) from None
            settle(usage, self.cost(usage), True, choice.get("finish_reason") or "unknown")
            qsettle(usage, "ok")
            if self.quota: self.quota.observe(self.quota_scope, resp.headers)
            self.version = redact(version) if version else None
            label = self.label
            if "STUB" in str(raw.get("system_fingerprint", "")).upper() or raw.get("_qa_stub") is True:
                label = f"STUB ENDPOINT ({self.name}/{self.model}; NOT real model reasoning)"
            latency = round((time.monotonic()-started)*1000, 3)
            self.metrics.update(latency_ms=latency)
            if self.name != "ollama" or "tokens_per_second" not in self.metrics:
                self.metrics["tokens_per_second"] = round(usage["completion_tokens"] / max(latency/1000, .001), 3)
            if self.name != "compatible": self.gateway.record(run_id, "model.identity", {"provider": self.name, "model": self.model, "version": self.version}, "allow", "selected model response received")
            return ModelResponse(text=text, provider=self.name, model=self.model, label=label,
                                 finish_reason=choice.get("finish_reason") or "unknown", usage=usage,
                                 cost_usd=self.cost(usage), priced=True, attempts=attempt+1, correlation_ids=cids,
                                 version=self.version, metrics=dict(self.metrics))
        raise ModelCallError("exhausted retries", kind="transport")

    def completion_path(self):
        return "/chat/completions"

    def decode(self, data):
        return data

    def headers(self, correlation_id=None):
        headers = {"Content-Type": "application/json", "X-Request-ID": correlation_id or ""}
        key = os.environ.get(self.key_env) if self.key_env else None
        if self.classification == "hosted" and not key:
            raise ProviderNotApproved("api key disappeared from environment")
        if key:
            register_secret_value(key)
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def discovery_path(self):
        return "/models"

    def discovered_models(self, data):
        return [{"id": m["id"], "version": m.get("version")} for m in data.get("data", []) if isinstance(m, dict) and isinstance(m.get("id"), str)]

    def request_metadata(self, method, path, body=None):
        if self.gateway is None:
            raise ProviderNotApproved("provider has no gateway")
        if self.name != "compatible" and self.model != self._configured_model:
            raise ProviderNotApproved("selected model changed after administrator configuration")
        if self.name != "compatible" and self.base_url != self._configured_url:
            raise ProviderNotApproved("endpoint changed after administrator configuration")
        if self.classification == "hosted" and not self.is_local_stub and os.environ.get("CI") and self.transport is None:
            raise ProviderNotApproved("hosted providers disabled in CI")
        url = self.base_url + path
        qid = self.quota.reserve(self.quota_scope, 0, self.quota_limits, self.timeout) if self.quota else None
        sent = False
        try:
            def request():
                nonlocal sent
                headers = self.headers()
                sent = True
                try:
                    with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False, trust_env=False) as c:
                        r = c.request(method, url, json=body, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    raise ModelCallError("model discovery endpoint unavailable", kind="timeout" if isinstance(e, httpx.TimeoutException) else "transport", retryable=True) from None
                if self.quota: self.quota.observe(self.quota_scope, r.headers)
                if r.status_code != 200:
                    kind, retryable = normalize_status(r.status_code)
                    raise ModelCallError(f"model metadata HTTP {r.status_code}", kind=kind, status=r.status_code, retryable=retryable)
                return r
            return self.gateway.invoke(None, "model.request", {"url": url, "provider": self.name, "model": self.model, "version": self.version, "task": "discovery"},
                                       request, {"provider_approved": self.provider_approved})
        finally:
            if self.quota: self.quota.settle(qid, 0, "metadata" if sent else "denied_not_sent")

    def prepare(self):
        available = self.discover()
        match = next((m for m in available if m["id"] == self.model), None)
        if match is None:
            raise ModelCallError("model compatibility error: selected model is not installed/available; no model is downloaded automatically", kind="not_found")
        self.version = match.get("version") or self.version

    def discover(self):
        try:
            return self.discovered_models(self.request_metadata("GET", self.discovery_path()).json())
        except (ValueError, KeyError, TypeError):
            raise ModelCallError("malformed model catalog", kind="invalid_response") from None

    def backoff(self, attempt):
        """Exponential backoff with bounded jitter: base*2^attempt * (1 +/- jitter), capped at max_backoff."""
        base = min(self.backoff_base * (2 ** attempt), self.max_backoff)
        return min(base * (1 + self.rng.uniform(-self.jitter, self.jitter)), self.max_backoff)

    def _post(self, url, body, correlation_id=None):
        with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False, trust_env=False) as c:
            r = c.post(url, json=body, headers=self.headers(correlation_id))
        if r.status_code != 200:
            raise _HTTPStatus(r.status_code, r.headers.get("retry-after"), redact(r.text), dict(r.headers))
        return r


class _HTTPStatus(Exception):
    def __init__(self, status, retry_after, body, headers=None):
        self.headers = headers or {}
        super().__init__(f"HTTP {status}")
        self.status, self.retry_after, self.body = status, retry_after, body or ""


def build_model_provider(agent_cfg: dict, root: Path, gateway=None, selected=None) -> ModelProvider:
    name = selected or os.environ.get("QA_MODEL_PROVIDER") or agent_cfg["model"]["provider"]
    pc = agent_cfg["providers"].get(name)
    if pc is None:
        raise ProviderNotApproved(f"unknown model provider {name!r}")
    if name == "replay":
        return ReplayModelProvider(root / pc["replay_dir"], pc.get("label", "REPLAYED (mock model)"))
    if name in ("compatible", "ollama", "lmstudio", "llamacpp", "vllm", "groq", "gemini", "openrouter", "nvidia"):
        from .local import OllamaProvider, GeminiProvider
        pc = dict(pc, provider_id=name)
        cls = OllamaProvider if name == "ollama" else GeminiProvider if name == "gemini" else OpenAICompatibleProvider
        return cls(pc, gateway=gateway)
    raise ProviderNotApproved(f"provider {name!r} has no implementation")
