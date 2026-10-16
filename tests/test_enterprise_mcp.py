"""Remote MCP contract/scope tests. All servers/credentials are synthetic MockTransport."""
import copy
import json
import httpx
import pytest

from qa_agent.integrations.mcp import RemoteMcpClient, McpContractError
from qa_agent.integrations.http import IntegrationNotAuthorized
from qa_agent.util import canon, sha256

pytestmark = [pytest.mark.security]


def schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


class Server:
    def __init__(self, *, sse=False, dispatcher=False):
        self.sse, self.dispatcher = sse, dispatcher
        self.requests, self.fault, self.event = [], None, None
        self.tool = {"name": "executeRead" if dispatcher else "getJiraIssue", "description": "Synthetic read contract",
                     "inputSchema": schema({"cloudId": {"const": "cloud-a"}, "issueIdOrKey": {"type": "string", "pattern": "^PCC-[0-9]+$"}})}
        if dispatcher:
            self.tool["inputSchema"] = schema({"tool": {"const": "getJiraIssue"}, "arguments": self.tool["inputSchema"]})

    def __call__(self, request):
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request, body))
        if self.fault:
            return httpx.Response(self.fault)
        if request.method == "DELETE":
            return httpx.Response(204)
        method = body.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-11-25", "serverInfo": {"name": "approved-server", "version": "1"}, "capabilities": {"tools": {}}}
            headers = {"mcp-session-id": "synthetic-session"}
        elif method == "notifications/initialized":
            return httpx.Response(202)
        elif method == "tools/list":
            result = {"tools": [self.tool]};headers = {}
        elif method == "tools/call":
            result = {"structuredContent": {"key": "PCC-1", "fields": {"project": {"key": "PCC"}}}};headers = {}
        else:
            raise AssertionError(method)
        message = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:
            payload = ""
            if self.event:
                payload += "data: " + json.dumps(self.event) + "\n\n"
            payload += "data: " + json.dumps(message) + "\n\n"
            return httpx.Response(200, text=payload, headers={**headers, "content-type": "text/event-stream"})
        return httpx.Response(200, json=message, headers=headers)


def config(server):
    properties = {"cloudId": {"const": "cloud-a"}, "issueIdOrKey": {"type": "string", "pattern": "^PCC-[0-9]+$"}}
    arguments = schema(properties)
    binding = {"tool": server.tool["name"], "tool_sha256": sha256(canon(server.tool)),
               "parameters_schema": schema({"key": {"type": "string", "pattern": "^PCC-[0-9]+$"}}),
               "parameters": {"key": "issueIdOrKey"}, "constants": {"cloudId": "cloud-a"}, "arguments_schema": arguments}
    if server.dispatcher:
        binding.update(constants={"tool": "getJiraIssue", "arguments": {"cloudId": "cloud-a"}}, argument_path=["arguments"],
                       inner_operation={"path": ["tool"], "value": "getJiraIssue"},
                       arguments_schema=schema({"tool": {"const": "getJiraIssue"}, "arguments": arguments}))
    return {"enabled": True, "approved": True, "approval_ref": "synthetic-only", "base_url": "https://mcp.example.invalid/mcp",
            "allowed_hosts": ["mcp.example.invalid"], "auth": {"type": "bearer", "token_env": "TEST_MCP_TOKEN"},
            "projects": ["PCC"], "protocol_version": "2025-11-25", "server_name": "approved-server", "server_version": "1",
            "operations": {"issue.read": binding}}


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setenv("TEST_MCP_TOKEN", "synthetic-mcp-key")


@pytest.mark.parametrize("sse,dispatcher", [(False, False), (True, False), (True, True)])
def test_pinned_read_contract_session_and_transport(sse, dispatcher):
    server = Server(sse=sse, dispatcher=dispatcher)
    client = RemoteMcpClient(config(server), transport=httpx.MockTransport(server))
    assert client.read("issue.read", {"key": "PCC-1"})["key"] == "PCC-1"
    client.close()
    methods = [body.get("method") for _, body in server.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call", None]
    assert server.requests[-1][0].method == "DELETE"
    assert all(request.headers.get("mcp-session-id") == "synthetic-session" for request, _ in server.requests[1:])
    assert not any("synthetic-mcp-key" in json.dumps(body) for _, body in server.requests)


@pytest.mark.parametrize("params", [{"key": "OTHER-1"}, {"key": "PCC-1", "cloudId": "cloud-b"}, {"key": "PCC-1", "tool": "deleteIssue"}])
def test_scope_and_operation_injection_is_denied_before_network(params):
    server = Server(dispatcher=True)
    client = RemoteMcpClient(config(server), transport=httpx.MockTransport(server))
    with pytest.raises(McpContractError, match="typed MCP parameters"):
        client.read("issue.read", params)
    assert not server.requests


def test_discovered_contract_drift_cannot_execute():
    server = Server()
    cfg = config(server)
    server.tool["description"] = "Changed tool or injected instructions"
    client = RemoteMcpClient(cfg, transport=httpx.MockTransport(server))
    with pytest.raises(McpContractError, match="contract changed"):
        client.read("issue.read", {"key": "PCC-1"})
    assert all(body.get("method") != "tools/call" for _, body in server.requests)


def test_generic_dispatch_cannot_replace_pinned_inner_operation():
    server = Server(dispatcher=True)
    cfg = config(server)
    binding = cfg["operations"]["issue.read"]
    binding["constants"]["tool"] = "deleteJiraIssue"
    # Even if a bad local schema allows it, the independent dispatch binding refuses.
    binding["arguments_schema"]["properties"]["tool"] = {"type": "string"}
    client = RemoteMcpClient(cfg, transport=httpx.MockTransport(server))
    with pytest.raises(McpContractError, match="inner operation"):
        client.read("issue.read", {"key": "PCC-1"})
    assert not server.requests


@pytest.mark.parametrize("tool", ["executeWrite", "executeDestructive", "deleteIssue", "runShell"])
def test_nonread_tools_cannot_be_configured_as_reads(tool):
    server = Server();cfg = config(server)
    cfg["operations"]["issue.read"]["tool"] = tool
    with pytest.raises(IntegrationNotAuthorized, match="read tool"):
        RemoteMcpClient(cfg, transport=httpx.MockTransport(server))
    assert not server.requests


@pytest.mark.parametrize("event", [
    {"jsonrpc": "2.0", "method": "sampling/createMessage", "id": 9, "params": {}},
    {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
])
def test_server_initiated_requests_or_changed_catalog_are_denied(event):
    server = Server(sse=True);server.event = event
    client = RemoteMcpClient(config(server), transport=httpx.MockTransport(server))
    with pytest.raises(McpContractError):
        client.read("issue.read", {"key": "PCC-1"})
    assert all(body.get("method") != "tools/call" for _, body in server.requests)


def test_remote_schema_cannot_fetch_external_references():
    server = Server();cfg = config(server)
    server.tool["inputSchema"] = {"$ref": "https://metadata.invalid/leak"}
    client = RemoteMcpClient(cfg, transport=httpx.MockTransport(server))
    with pytest.raises(McpContractError, match="external JSON"):
        client.discover()


def test_auth_rejection_does_not_retry_or_log_response_body():
    server = Server();server.fault = 401
    client = RemoteMcpClient(config(server), transport=httpx.MockTransport(server))
    with pytest.raises(IntegrationNotAuthorized, match="reconnect"):
        client.read("issue.read", {"key": "PCC-1"})
    assert len(server.requests) == 1 and not client.catalog


def test_open_sse_stream_is_closed_after_matching_response_without_waiting_for_eof():
    server = Server()
    class LiveStream(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            message = {"jsonrpc": "2.0", "id": 3, "result": {"structuredContent": {"key": "PCC-1"}}}
            raw = ("data: " + json.dumps(message) + "\r\n\r\n").encode()
            # Deliberately split CRLF/event delimiters across transport chunks.
            for b in raw:
                yield bytes([b])
            raise AssertionError("client read beyond its completed response")
        def close(self):
            self.closed = True
    stream = LiveStream()
    def handler(request):
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})
        return server(request)
    client = RemoteMcpClient(config(server), transport=httpx.MockTransport(handler))
    assert client.read("issue.read", {"key": "PCC-1"}) == {"key": "PCC-1"}
    assert stream.closed
