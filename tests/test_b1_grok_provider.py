"""GrokProvider against recorded-shape responses (httpx.MockTransport). No network: a socket guard fails the
test if anything tries to connect to a non-loopback address."""
import json, socket
import httpx, pytest
from qa_agent import ROOT
from qa_agent.gateway import PolicyDenied
from qa_agent.planning import PLAN_STORY_SCHEMA, parse_output, FormatError
from qa_agent.providers.models import (GrokProvider, ProviderNotApproved, ProviderConfigError, DataClassDenied,
                                       ModelCallError, UsageMissing, build_model_provider)
from qa_agent.runtime import QAAgent
from qa_agent.util import load_json, redact
from tests.b1_helpers import grok_cfg, fake_key

REC = ROOT / "fixtures/model_recordings"
CTX = {"story_key": "PCC-DEMO-2", "data_class": "synthetic", "story": load_json(ROOT / "fixtures/jira/PCC-DEMO-2.json"), "catalog": {}}


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    real = socket.socket.connect

    def guard(self, addr):
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"EXTERNAL CONNECTION ATTEMPTED to {addr}")
        return real(self, addr)
    monkeypatch.setattr(socket.socket, "connect", guard)


@pytest.fixture
def key(monkeypatch):
    k = fake_key()
    monkeypatch.setenv("XAI_API_KEY", k)
    return k


@pytest.fixture
def agent(tmp_path):
    return QAAgent(tmp_path)   # replay provider; we use its gateway/store


class Script:
    """MockTransport that replays a scripted list of responses and records requests."""
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def __call__(self, request):
        self.calls.append(request)
        it = self.items.pop(0) if self.items else ("json", 200, ok_body())
        if it == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        kind, status, payload, *hdr = it
        return httpx.Response(status, json=payload, headers=(hdr[0] if hdr else {}))


def ok_body(content='{"a":1}'):
    b = load_json(REC / "chat_completion_ok.json")
    b["choices"][0]["message"]["content"] = content
    return b


def provider(agent, script, base="http://127.0.0.1:9/v1", sleeps=None, **over):
    return GrokProvider(grok_cfg(base, **over), gateway=agent.gateway, transport=httpx.MockTransport(script),
                        sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_request_shape_structured_output(agent, key):
    s = Script(("json", 200, ok_body()))
    r = provider(agent, s).generate("plan_story", CTX, PLAN_STORY_SCHEMA, run_id="r1")
    req = s.calls[0]
    body = json.loads(req.content)
    assert str(req.url) == "http://127.0.0.1:9/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {key}"
    assert body["model"] == "grok-4.7" and body["max_completion_tokens"] == 8000
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == PLAN_STORY_SCHEMA
    assert "<untrusted_story>" in body["messages"][1]["content"] and "UNTRUSTED DATA" in body["messages"][0]["content"]
    assert r.text == '{"a":1}' and "STUB ENDPOINT" in r.label


def test_usage_and_cost_from_recorded_response(agent, key):
    r = provider(agent, Script(("json", 200, ok_body()))).generate("plan_story", CTX, {}, run_id="r1")
    assert r.usage == {"prompt_tokens": 1200, "cached_tokens": 200, "completion_tokens": 900, "reasoning_tokens": 500,
                       "total_tokens": 2600, "output_billable_tokens": 1400}
    assert r.cost_usd == pytest.approx((1000 * 2.0 + 200 * 0.5 + 1400 * 6.0) / 1e6)


def test_429_retry_after_honoured(agent, key):
    sleeps = []
    s = Script(("json", 429, {"error": "rl"}, {"Retry-After": "0.03"}), ("json", 200, ok_body()))
    r = provider(agent, s, sleeps=sleeps).generate("plan_story", CTX, {}, run_id="r1")
    assert r.attempts == 2 and sleeps == [0.03] and len(s.calls) == 2


def test_5xx_exponential_backoff_then_exhaustion(agent, key):
    sleeps = []
    s = Script(*[("json", 503, {"e": 1})] * 4)
    with pytest.raises(ModelCallError, match="HTTP 503"):
        provider(agent, s, sleeps=sleeps, max_retries=3).generate("plan_story", CTX, {}, run_id="r1")
    assert len(s.calls) == 4 and len(sleeps) == 3 and sleeps[0] < sleeps[1] < sleeps[2] <= 0.05 * 1.1


def test_non_retryable_status_not_retried(agent, key):
    s = Script(("json", 400, {"error": "bad request"}))
    with pytest.raises(ModelCallError, match="HTTP 400"):
        provider(agent, s).generate("plan_story", CTX, {}, run_id="r1")
    assert len(s.calls) == 1


def test_timeout_retried_then_fails(agent, key):
    s = Script("timeout", "timeout", "timeout")
    with pytest.raises(ModelCallError, match="unreachable"):
        provider(agent, s, max_retries=2).generate("plan_story", CTX, {}, run_id="r1")
    assert len(s.calls) == 3


def test_truncated_and_empty_output_are_format_errors(agent, key):
    t = load_json(REC / "chat_completion_truncated.json")
    r = provider(agent, Script(("json", 200, t))).generate("plan_story", CTX, {}, run_id="r1")
    with pytest.raises(FormatError, match="truncated"):
        parse_output(r.text, r.finish_reason)
    r = provider(agent, Script(("json", 200, ok_body("")))).generate("plan_story", CTX, {}, run_id="r1")
    with pytest.raises(FormatError, match="empty"):
        parse_output(r.text, r.finish_reason)


def test_missing_usage_fails_closed(agent, key):
    with pytest.raises(UsageMissing):
        provider(agent, Script(("json", 200, load_json(REC / "chat_completion_no_usage.json")))).generate("plan_story", CTX, {}, run_id="r1")


def test_constructor_fail_closed_matrix(key, monkeypatch):
    base = "http://127.0.0.1:9/v1"
    with pytest.raises(ProviderNotApproved, match="not enabled"):
        GrokProvider(grok_cfg(base, enabled=False))
    with pytest.raises(ProviderConfigError, match="pricing"):
        GrokProvider(grok_cfg(base, pricing={"input_per_mtok_usd": 1, "cached_input_per_mtok_usd": None, "output_per_mtok_usd": 2}))
    with pytest.raises(ProviderNotApproved, match="not approved for external endpoint api.x.ai"):
        GrokProvider(grok_cfg("https://api.x.ai/v1"))
    with pytest.raises(ProviderNotApproved, match="not approved"):
        GrokProvider(grok_cfg("https://api.x.ai/v1", approved=True))          # approval_ref missing
    monkeypatch.delenv("XAI_API_KEY")
    with pytest.raises(ProviderNotApproved, match="api key"):
        GrokProvider(grok_cfg(base))


def test_data_class_gate_blocks_before_any_request(agent, key):
    s = Script()
    with pytest.raises(DataClassDenied):
        provider(agent, s).generate("plan_story", dict(CTX, data_class="confidential"), {}, run_id="r1")
    assert s.calls == []


def test_egress_to_api_x_ai_blocked_unless_approved(agent, key):
    s = Script(("json", 200, ok_body()))
    p = provider(agent, s)
    p.base_url = "https://api.x.ai/v1"          # simulate a runtime misconfiguration / bypass attempt
    with pytest.raises(PolicyDenied, match="provider not approved"):
        p.generate("plan_story", CTX, {}, run_id="r-egress")
    assert s.calls == []                         # nothing left the process
    row = agent.store.audit_rows("r-egress")[-1]
    assert row["tool"] == "model.request" and row["decision"] == "deny"
    p.base_url = "https://evil.example/v1"
    with pytest.raises(PolicyDenied, match="not in approved model hosts"):
        p.generate("plan_story", CTX, {}, run_id="r-egress")
    # with the approved flag + approval_ref the same host is allowed (MockTransport => still no real network)
    s2 = Script(("json", 200, ok_body()))
    p2 = GrokProvider(grok_cfg("https://api.x.ai/v1", approved=True, approval_ref="TEST-ONLY"), gateway=agent.gateway,
                      transport=httpx.MockTransport(s2), sleep=lambda s: None)
    assert "LIVE MODEL" in p2.label
    p2.generate("plan_story", CTX, {}, run_id="r-egress")
    assert len(s2.calls) == 1 and str(s2.calls[0].url).startswith("https://api.x.ai/")


def test_no_gateway_no_call(key):
    s = Script()
    p = GrokProvider(grok_cfg("http://127.0.0.1:9/v1"), transport=httpx.MockTransport(s))
    with pytest.raises(ProviderNotApproved, match="gateway"):
        p.generate("plan_story", CTX, {}, run_id="r1")
    assert s.calls == []


def test_secret_redacted_from_errors_and_audit(agent, key):
    s = Script(("json", 401, {"error": f"invalid key Bearer {key}"}))
    with pytest.raises(ModelCallError) as e:
        provider(agent, s).generate("plan_story", CTX, {}, run_id="r-sec")
    assert key not in str(e.value)
    dump = json.dumps(agent.store.audit_rows("r-sec")) + (agent.artifacts.path_for("r-sec", "audit.jsonl")).read_text()
    assert key not in dump and "model.request" in dump
    assert redact(f"token={key} and Bearer abcdefgh12345") == "token=[REDACTED] and Bearer [REDACTED]"


def test_no_silent_fallback_to_other_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("QA_MODEL_PROVIDER", "grok")
    with pytest.raises(ProviderNotApproved):
        QAAgent(tmp_path)          # grok misconfigured -> agent refuses to start; does NOT fall back to replay
