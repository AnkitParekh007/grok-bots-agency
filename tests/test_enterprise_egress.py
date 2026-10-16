"""Real local network observations: denied destinations must receive zero requests."""
import contextlib
import ipaddress
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from qa_agent import ROOT
from qa_agent.executor import PlaywrightExecutor
from qa_agent.gateway import PolicyDenied
from qa_agent.network import EgressProxy
from qa_agent.runtime import QAAgent
from tests.b1_helpers import agent_cfg

pytestmark = [pytest.mark.security, pytest.mark.execution]


@contextlib.contextmanager
def server(responder):
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            status, headers, body = responder(self.path)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", hits
    finally:
        http.shutdown()
        http.server_close()
        thread.join(5)


def agent(tmp_path):
    a = QAAgent(tmp_path, agent_cfg=agent_cfg("replay"))
    a.store.create_run("run-proxy", "synthetic network test", "synthetic-local", "replay", "replay")
    a.approvals.record("run-proxy", "fixture-hash", "synthetic-reviewer", "approved")
    return a


def environment(url):
    return {"name": "synthetic-local", "base_url": url, "allowed_hosts": ["127.0.0.1"],
            "status": "verified-synthetic", "production": False}


@pytest.mark.integration
@pytest.mark.parametrize("attack", ["image", "browser_chain", "api_chain"])
def test_real_executor_blocks_resources_and_every_redirect_hop(tmp_path, attack):
    with server(lambda p: (200, {}, b"forbidden sink")) as (sink, sink_hits):
        def respond(path):
            if path == "/a":
                return 302, {"Location": "/b"}, b""
            if path == "/b":
                return 302, {"Location": sink + "/forbidden"}, b""
            return 200, {"Content-Type": "text/html"}, f'<img src="{sink}/image">'.encode()
        with server(respond) as (source, source_hits):
            a = agent(tmp_path)
            def action(t, params):
                if attack == "api_chain":
                    t.http("GET", "/a")
                else:
                    t.goto("/a" if attack == "browser_chain" else "/")
                    t.page.wait_for_load_state("networkidle")
                return {"ok": True}
            skills = SimpleNamespace(get=lambda name: SimpleNamespace(actions={"probe": ("browser", action)}))
            ex = PlaywrightExecutor(a.gateway, a.artifacts, skills, {"oracle": {"expected": {"ok": True}}},
                                    ROOT / "fixtures/data", a.store)
            tc = {"id": "TC-1", "title": "network probe", "type": "browser", "skill": "probe", "action": "probe",
                  "oracle_ref": "oracle", "ac_refs": ["AC1"], "params": {}, "category": "negative"}
            results = ex.run("run-proxy", {"test_cases": [tc]}, environment(source), lambda *a: None,
                             plan_hash="fixture-hash")
            assert results[0]["status"] == "blocked", results
            assert sink_hits == [], "A policy-denied destination received traffic"
            assert source_hits, "Probe must actually exercise the allowed target"
            if attack != "image":
                assert "/b" in source_hits, "The second allowed redirect hop must be exercised"
            assert any(r["decision"] == "deny" for r in a.store.audit_rows("run-proxy"))
            a.store.db.close()


@pytest.mark.integration
def test_proxy_preserves_allowed_traffic_and_refuses_other_ports(tmp_path):
    a = agent(tmp_path)
    with server(lambda p: (200, {"Content-Type": "application/json"}, b'{"ok":true}')) as (source, hits):
        with server(lambda p: (200, {}, b"forbidden")) as (sink, sink_hits):
            with EgressProxy(a.gateway, "run-proxy", environment(source), "fixture-hash") as proxy:
                with httpx.Client(proxy=proxy.url, trust_env=False) as client:
                    assert client.get(source + "/allowed").json() == {"ok": True}
                    assert client.get(sink + "/denied").status_code == 403
            assert hits == ["/allowed"] and not sink_hits
    a.store.db.close()


def test_resolver_rebinding_is_refused_before_socket(tmp_path, monkeypatch):
    a = agent(tmp_path)
    env = {"name": "dev", "base_url": "https://approved.example", "allowed_hosts": ["approved.example"],
           "status": "verified"}
    for address in ("127.0.0.1", "169.254.169.254", "10.0.0.8", "::ffff:127.0.0.1"):
        def resolver(*args, **kwargs):
            return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
        proxy = EgressProxy(a.gateway, "run-proxy", env, "fixture-hash", resolver=resolver)
        with pytest.raises(PolicyDenied, match="network ranges"):
            proxy.destination("https://approved.example/file", "CONNECT")
    a.store.db.close()


def test_only_explicit_private_ranges_can_be_resolved(tmp_path):
    a = agent(tmp_path)
    env = {"base_url": "https://dev.example", "allowed_hosts": ["dev.example"], "status": "verified",
           "allowed_cidrs": ["10.5.0.0/24"]}
    proxy = EgressProxy(a.gateway, "run-proxy", env, "fixture-hash",
                       resolver=lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.5.0.8", 443))])
    assert proxy.destination("https://dev.example/", "CONNECT")[2][0][0] == "10.5.0.8"
    a.store.db.close()
