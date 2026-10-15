"""Native Ollama and OpenAI-compatible local engines. Never pulls a model."""
import copy
from .models import OpenAICompatibleProvider, ModelCallError, ProviderConfigError


class OllamaProvider(OpenAICompatibleProvider):
    def build_body(self, task, context, schema, repair_feedback=None):
        from ..planning import build_messages
        return {"model": self.model, "messages": build_messages(task, context, repair_feedback),
                "stream": False, "format": schema,
                "options": {"num_ctx": self.context_window, "num_predict": self.max_completion_tokens}}

    def completion_path(self):
        return "/api/chat"

    def decode(self, data):
        if not isinstance(data.get("message"), dict):
            raise ModelCallError("Ollama returned no message", kind="invalid_response")
        normalized = {"model": data.get("model"), "model_version": self.version,
                      "choices": [{"message": data["message"], "finish_reason": "length" if data.get("done_reason") == "length" else ("stop" if data.get("done") else "unknown")}],
                      "usage": {"prompt_tokens": data.get("prompt_eval_count"), "completion_tokens": data.get("eval_count")}}
        if data.get("eval_duration") and data.get("eval_count"):
            self.metrics["tokens_per_second"] = round(data["eval_count"] / (data["eval_duration"] / 1e9), 3)
        return normalized

    def discovery_path(self):
        return "/api/tags"

    def discovered_models(self, data):
        return [{"id": m.get("name"), "version": m.get("digest"), "size_bytes": m.get("size")}
                for m in data.get("models", []) if isinstance(m, dict) and isinstance(m.get("name"), str)]

    def discover(self):
        models = super().discover()
        match = next((m for m in models if m["id"] == self.model), None)
        if match and match.get("version"):
            self.version = match["version"]
        return models

    def prepare(self):
        super().prepare()
        self.inspect_model()

    def inspect_model(self):
        """Explicit read-only capability probe; /api/show never downloads a model."""
        data = self.request_metadata("POST", "/api/show", {"model": self.model}).json()
        capabilities = data.get("capabilities") or []
        if "completion" not in capabilities:
            raise ProviderConfigError("model compatibility error: Ollama model does not support completion")
        windows = [v for k, v in (data.get("model_info") or {}).items()
                   if k.endswith(".context_length") and isinstance(v, int)]
        if windows and self.context_window > max(windows):
            raise ProviderConfigError("model compatibility error: configured context exceeds model context length")
        return {"capabilities": capabilities, "context_window": max(windows) if windows else None}

    def memory(self):
        data = self.request_metadata("GET", "/api/ps").json()
        match = next((m for m in data.get("models", []) if m.get("name") == self.model), {})
        return {"memory_bytes": match.get("size"), "vram_bytes": match.get("size_vram")}


class GeminiProvider(OpenAICompatibleProvider):
    """Official generateContent protocol, not the OpenAI compatibility endpoint."""
    def completion_path(self):
        from urllib.parse import quote
        return f"/models/{quote(self.model, safe='')}:generateContent"

    def headers(self, correlation_id=None):
        headers = super().headers(correlation_id)
        key = headers.pop("Authorization", "").removeprefix("Bearer ")
        headers["x-goog-api-key"] = key
        return headers

    def build_body(self, task, context, schema, repair_feedback=None):
        from ..planning import build_messages
        messages = build_messages(task, context, repair_feedback)
        # Gemini supports a JSON Schema subset. Runtime validation remains authoritative.
        schema = copy.deepcopy(schema)
        def clean(obj):
            if isinstance(obj, dict):
                obj.pop("$schema", None)
                for v in obj.values(): clean(v)
            elif isinstance(obj, list):
                for v in obj: clean(v)
        clean(schema)
        return {"systemInstruction": {"parts": [{"text": messages[0]["content"]}]},
                "contents": [{"role": "user", "parts": [{"text": m["content"]}]} for m in messages[1:]],
                "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema,
                                     "maxOutputTokens": self.max_completion_tokens}}

    def decode(self, data):
        candidates = data.get("candidates") or []
        if not candidates:
            raise ModelCallError("Gemini returned no candidate (possibly safety blocked)", kind="invalid_response")
        c = candidates[0]
        u = data.get("usageMetadata") or {}
        # Include reasoning tokens in output usage; no provider-specific billing inference.
        output = u.get("candidatesTokenCount")
        thoughts = u.get("thoughtsTokenCount", 0)
        if isinstance(output, int) and isinstance(thoughts, int):
            output += thoughts
        return {"model": self.model, "model_version": data.get("modelVersion"),
                "choices": [{"message": {"content": "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []) if not p.get("thought"))},
                             "finish_reason": {"STOP": "stop", "MAX_TOKENS": "length"}.get(c.get("finishReason"), "unknown")}],
                "usage": {"prompt_tokens": u.get("promptTokenCount"), "completion_tokens": output,
                          "total_tokens": u.get("totalTokenCount")}}

    def discovered_models(self, data):
        return [{"id": m["name"].removeprefix("models/"), "version": m.get("version"),
                 "context_window": m.get("inputTokenLimit"), "output_limit": m.get("outputTokenLimit")}
                for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
