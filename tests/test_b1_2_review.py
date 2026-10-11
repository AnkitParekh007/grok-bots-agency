"""Regression tests for PR #1 review findings (egress, retry/concurrent budget, run-level access)."""
import json, socket, threading
import httpx, pytest
from fastapi.testclient import TestClient
from qa_agent import ROOT
from qa_agent.gateway import PolicyDenied
from qa_agent.providers.models import GrokProvider, ProviderConfigError
from qa_agent.runtime import QAAgent
from qa_agent.util import load_json
from tests.b1_helpers import grok_cfg, fake_key

CTX = {"story_key": "PCC-DEMO-2", "data_class": "synthetic", "story": load_json(ROOT / "fixtures/jira/PCC-DEMO-2.json"), "catalog": {}}


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    real = socket.socket.connect
    def guard(self, addr):
        if isinstance(addr, tuple) and addr[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"external network attempted: {addr}")
        return real(self, addr)
    monkeypatch.setattr(socket.socket, "connect", guard)


@pytest.fixture
def key(monkeypatch):
    k = fake_key(); monkeypatch.setenv("XAI_API_KEY", k); return k


def ok_body():
    b = load_json(ROOT / "fixtures/model_recordings/chat_completion_ok.json"); b["choices"][0]["message"]["content"] = "{}"; return b


class Script:
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def __call__(self, req):
        self.calls.append(req)
        st, body = self.items.pop(0)
        return httpx.Response(st, json=body)


APPROVED = dict(approved=True, approval_ref="TEST-ONLY")


# ---------------- egress: https + policy-owned allowlist ----------------
@pytest.mark.provider
@pytest.mark.security
def test_non_loopback_http_base_url_rejected_at_construction(key):
    with pytest.raises(ProviderConfigError, match="https"):
        GrokProvider(grok_cfg("http://api.x.ai/v1", **APPROVED))
    GrokProvider(grok_cfg("http://127.0.0.1:9/v1"))      # loopback stub may use http


@pytest.mark.provider
@pytest.mark.security
def test_gateway_denies_http_even_if_provider_check_bypassed(tmp_path, key):
    a = QAAgent(tmp_path); s = Script((200, ok_body()))
    p = GrokProvider(grok_cfg("https://api.x.ai/v1", **APPROVED), gateway=a.gateway, transport=httpx.MockTransport(s), sleep=lambda x: None)
    p.base_url = "http://api.x.ai/v1"                    # downgrade after construction
    with pytest.raises(PolicyDenied, match="must use https"):
        p.generate("plan_story", CTX, {}, run_id="r-http")
    assert s.calls == [] and a.store.audit_rows("r-http")[-1]["decision"] == "deny"


@pytest.mark.provider
@pytest.mark.security
def test_provider_config_cannot_extend_host_allowlist(tmp_path, key):
    a = QAAgent(tmp_path); s = Script((200, ok_body()))
    cfg = grok_cfg("https://evil.example/v1", **APPROVED, approved_model_hosts=["evil.example"])   # ignored
    p = GrokProvider(cfg, gateway=a.gateway, transport=httpx.MockTransport(s), sleep=lambda x: None)
    with pytest.raises(PolicyDenied, match="not in policy approved_hosts"):
        p.generate("plan_story", CTX, {}, run_id="r-host")
    assert s.calls == []
    assert load_json(ROOT / "config/policy.json")["tools"]["model.request"]["approved_hosts"] == ["api.x.ai"]
    assert "approved_model_hosts" not in load_json(ROOT / "config/agent.json")["providers"]["grok"]
