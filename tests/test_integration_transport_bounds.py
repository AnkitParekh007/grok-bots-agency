"""Offline transport faults; never contact the configured synthetic host."""
import json
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest

from qa_agent.integrations.http import ReadOnlyHttpClient, IntegrationError
from qa_agent.runtime import QAAgent
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.security, pytest.mark.provider]


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("READINESS_TRANSPORT_TEST_TOKEN", "synthetic-test-token-not-secret")
    return {"enabled": True, "approved": True, "approval_ref": "synthetic-test",
            "base_url": "https://transport.invalid", "allowed_hosts": ["transport.invalid"], "projects": ["PCC"],
            "auth": {"type": "bearer", "token_env": "READINESS_TRANSPORT_TEST_TOKEN"},
            "max_response_bytes": 1024, "max_retries": 2, "rate_limit_scope": uuid.uuid4().hex}


def test_chunked_body_is_stopped_before_all_chunks_are_consumed(config):
    consumed = []
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(20):
                consumed.append(1024)
                yield b"x" * 1024
    c = ReadOnlyHttpClient("test", config, transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Chunks())))
    try:
        with pytest.raises(IntegrationError, match="max_response_bytes"):
            c.get_json("fixture")
        assert sum(consumed) <= 2048
    finally:
        c._client.close()


@pytest.mark.parametrize("headers", [{"Content-Length": "999999999"}, {"Content-Encoding": "gzip"},
                                      {"Content-Length": "-1"}, {"Content-Length": "invalid"}])
def test_unsafe_headers_are_rejected_before_body_read(config, headers):
    consumed = []
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            consumed.append(True)
            yield b"untrusted body"
    c = ReadOnlyHttpClient("test", config, transport=httpx.MockTransport(lambda req: httpx.Response(200, headers=headers, stream=Body())))
    try:
        with pytest.raises(IntegrationError):
            c.get_json("fixture")
        assert consumed == []
    finally:
        c._client.close()


def test_proxy_environment_is_not_inherited(config, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    c = ReadOnlyHttpClient("test", config, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True})))
    try:
        assert c._client._trust_env is False
        assert c.get_json("fixture") == {"ok": True}
    finally:
        c._client.close()


@pytest.mark.parametrize("date_header", [False, True])
def test_long_cooldown_persists_and_prevents_other_client_network(config, tmp_path, date_header):
    a = QAAgent(tmp_path, agent_cfg=agent_cfg("replay"))
    clock = lambda: 1000.0
    header = format_datetime(datetime.fromtimestamp(4600, timezone.utc), usegmt=True) if date_header else "3600"
    calls = []
    def respond(req):
        calls.append(req.url)
        return httpx.Response(429, headers={"Retry-After": header})
    one = ReadOnlyHttpClient("test", config, gateway=a.gateway, clock=clock,
                            transport=httpx.MockTransport(respond), sleep=lambda x: pytest.fail("long cooldown must not sleep"))
    try:
        with pytest.raises(IntegrationError, match="retry later"):
            one.get_json("fixture")
        two = ReadOnlyHttpClient("test", config, gateway=a.gateway, clock=clock, transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(IntegrationError, match="cooldown active"):
                two.get_json("fixture")
            assert len(calls) == 1
        finally:
            two._client.close()
        assert a.store.q("SELECT blocked_until FROM integration_cooldowns")[0]["blocked_until"] == 4600
    finally:
        one._client.close()
        a.store.db.close()


def test_zero_retry_after_permits_bounded_retry_and_invalid_json_is_safe(config):
    calls = []
    def respond(req):
        calls.append(True)
        return httpx.Response(503, headers={"Retry-After": "0"}) if len(calls) == 1 else httpx.Response(200, content=b"not JSON")
    c = ReadOnlyHttpClient("test", config, transport=httpx.MockTransport(respond), sleep=lambda _: None)
    try:
        with pytest.raises(IntegrationError, match="invalid JSON response"):
            c.get_json("fixture")
        assert len(calls) == 2
    finally:
        c._client.close()
