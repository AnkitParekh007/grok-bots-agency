"""Bounded remote MCP reads with operator-pinned server, protocol and tool contracts.

No subprocesses, roots, sampling, elicitation, arbitrary tools or discovered writes.
Discovery produces review material; it never grants a tool permission.
"""
import json
import re
import threading
import time
import uuid
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator

from .http import IntegrationError, IntegrationNotAuthorized, ReadOnlyHttpClient
from ..util import canon, sha256
from ..jobs import JobConflict

READ_NAME = re.compile(r"(?:get|list|search|fetch|read)[A-Z_].*|executeRead")
PROTOCOLS = {"2025-03-26", "2025-06-18", "2025-11-25"}


def _validator(schema):
    """Schema validation must never fetch a reference supplied by a remote server."""
    pending, visited = [schema], 0
    while pending:
        visited += 1
        if visited > 10000:
            raise McpContractError("JSON schema exceeds complexity limit")
        item = pending.pop()
        if isinstance(item, dict):
            for key in ("$ref", "$dynamicRef"):
                if key in item and (not isinstance(item[key], str) or not item[key].startswith("#")):
                    raise McpContractError("external JSON schema references are not permitted")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


class McpContractError(IntegrationError):
    pass


class RemoteMcpClient:
    def __init__(self, cfg, gateway=None, *, transport=None):
        self.cfg, self.gateway = cfg, gateway
        self.endpoint = cfg.get("base_url", "")
        parsed = urlsplit(self.endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise IntegrationNotAuthorized("MCP endpoint must not contain credentials, query or fragment")
        if cfg.get("protocol_version") not in PROTOCOLS or not cfg.get("server_name") or not cfg.get("server_version"):
            raise IntegrationNotAuthorized("MCP requires an approved protocol and exact server identity/version")
        # Reuse approved URL/auth/TLS/cooldown configuration validation, never its GET transport.
        self.transport = ReadOnlyHttpClient("mcp", cfg, gateway, transport=transport)
        self.http = self.transport._client
        self.lock = threading.RLock()
        self.session, self.catalog, self.serial = None, {}, 0
        self.initialized = False
        self.auth_fingerprint = None
        self.operations = cfg.get("operations", {})
        if not isinstance(self.operations, dict):
            raise IntegrationNotAuthorized("invalid MCP operation map")
        for operation, binding in self.operations.items():
            if not isinstance(binding, dict) or not READ_NAME.fullmatch(binding.get("tool", "")):
                raise IntegrationNotAuthorized("MCP operation is not a permitted read tool")
            if not re.fullmatch(r"[a-z]+\.[a-z_]+", operation):
                raise IntegrationNotAuthorized("invalid typed MCP operation")
            if not re.fullmatch(r"[a-f0-9]{64}", binding.get("tool_sha256", "")):
                raise IntegrationNotAuthorized("MCP tool contract must have a reviewed SHA-256")
            schema = binding.get("arguments_schema")
            if not isinstance(schema, dict) or schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise IntegrationNotAuthorized("MCP operation requires a closed argument schema")
            _validator(schema)
            local = binding.get("parameters_schema", {"type": "object", "additionalProperties": False})
            if local.get("type") != "object" or local.get("additionalProperties") is not False:
                raise IntegrationNotAuthorized("MCP parameters require a closed schema")
            _validator(local)
            if binding["tool"] == "executeRead" and not binding.get("inner_operation"):
                raise IntegrationNotAuthorized("generic MCP dispatch requires an exact inner operation binding")

    def close(self):
        # Session IDs expire server-side too. A failed DELETE never triggers replay or another URL.
        with self.lock:
            if self.session:
                try:
                    self._exchange("DELETE", None, notification=True)
                except (IntegrationError, JobConflict):
                    pass
            self.session, self.catalog = None, {}
            self.http.close()

    def _headers(self):
        headers = self.transport._auth_headers()
        fingerprint = sha256(canon(headers))
        if self.auth_fingerprint is not None and fingerprint != self.auth_fingerprint:
            self.session, self.catalog = None, {}
            self.initialized = False
        self.auth_fingerprint = fingerprint
        return {**headers, "Accept": "application/json, text/event-stream", "Accept-Encoding": "identity",
                "MCP-Protocol-Version": self.cfg["protocol_version"],
                **({"MCP-Session-Id": self.session} if self.session else {})}

    def _exchange(self, method, message, *, notification=False):
        self._worker_guard()
        args = {"method": method, "rpc": message.get("method") if message else "session.close",
                "endpoint_sha256": sha256(self.endpoint), "request_id": uuid.uuid4().hex}
        run_id = self.gateway.store.job_run_id() if self.gateway else None
        if self.gateway:
            reason = self.gateway.check(run_id, "mcp.read", {"operation": "protocol"}, None)
            if reason:
                self.gateway.record(run_id, "mcp.transport", args, "deny", "MCP protocol denied by application policy")
                raise IntegrationNotAuthorized("MCP protocol denied by application policy")
            denial = self.gateway.audit_delivery.admission_denial(self.gateway.policy.get("audit_delivery"))
            if denial:
                raise IntegrationError(denial)
            self.gateway.record(run_id, "mcp.transport.intent", args, "allow", "approved endpoint; durable protocol request intent", "pending")
        try:
            self._worker_guard()
            result = self._exchange_raw(method, message, notification=notification)
            self._worker_guard()
        except Exception as exc:
            if self.gateway:
                self.gateway.record(run_id, "mcp.transport", args, "allow", "protocol request stopped or rejected", type(exc).__name__)
            raise
        if self.gateway:
            self.gateway.record(run_id, "mcp.transport", args, "allow", "bounded protocol response", "ok")
        return result

    def _worker_guard(self):
        if self.gateway:
            self.gateway.store.assert_job_lease()

    def _exchange_raw(self, method, message, *, notification=False):
        headers = self._headers()
        if self.transport._cooldown_until() > self.transport.clock():
            raise IntegrationError("MCP upstream cooldown active; retry later", 429)
        started = time.monotonic()
        try:
            with self.http.stream(method, self.endpoint, json=message, headers=headers) as response:
                if response.status_code == 429:
                    wait = self.transport._retry_after(response.headers.get("retry-after"))
                    self.transport._remember_cooldown(wait if wait is not None else 60)
                if response.status_code in (401, 403):
                    self.session, self.catalog = None, {}
                    self.initialized = False
                    raise IntegrationNotAuthorized("MCP credential rejected; reconnect required", response.status_code)
                if response.status_code == 404 and self.session:
                    self.session, self.catalog = None, {}
                    self.initialized = False
                    raise IntegrationError("MCP session expired; reconnect required", 404)
                if notification and response.status_code in (200, 202, 204, 405):
                    return None
                if response.status_code != 200:
                    raise IntegrationError("MCP request failed", response.status_code)
                session = response.headers.get("mcp-session-id")
                if session:
                    if message is None or message.get("method") != "initialize" or not re.fullmatch(r"[\x21-\x7e]{1,256}", session):
                        raise McpContractError("unexpected MCP session identifier")
                    self.session = session
                if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                    raise McpContractError("compressed MCP responses are not permitted")
                body = bytearray()
                media = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if int(response.headers.get("content-length", "0")) > self.transport.max_bytes:
                    raise McpContractError("MCP response exceeds byte limit")
                sse_pending, complete_events, event_count = bytearray(), [], 0
                chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    self._worker_guard()
                    if time.monotonic() - started > self.transport.timeout:
                        raise McpContractError("MCP response deadline exceeded")
                    if len(body) + len(chunk) > self.transport.max_bytes:
                        raise McpContractError("MCP response exceeds byte limit")
                    body.extend(chunk)
                    if media == "text/event-stream":
                        # A successful response can precede the end of an open SSE stream.
                        # Consume complete events only, bounded by the same byte/deadline limits.
                        sse_pending.extend(chunk)
                        # Normalize complete CRLFs; a trailing CR stays pending until its LF arrives.
                        normalized = bytes(sse_pending).replace(b"\r\n", b"\n")
                        segments = normalized.split(b"\n\n")
                        complete, remainder = segments[:-1], segments[-1]
                        sse_pending = bytearray(remainder)
                        found = False
                        for event in complete:
                            event_count += 1
                            if event_count > 128:
                                raise McpContractError("MCP event limit exceeded")
                            complete_events.append(event)
                            data = b"\n".join(line[5:].lstrip(b" ") for line in event.splitlines() if line.startswith(b"data:"))
                            if data:
                                value = json.loads(data)
                                if isinstance(value, dict) and "method" not in value and value.get("id") == message.get("id"):
                                    found = True
                        if found:
                            body = bytearray(b"\n\n".join(complete_events) + b"\n\n")
                            break
                if media == "application/json":
                    messages = [json.loads(body)]
                elif media == "text/event-stream":
                    text = body.decode("utf-8").replace("\r\n", "\n")
                    events = text.split("\n\n")
                    if len(events) > 128:
                        raise McpContractError("MCP event limit exceeded")
                    messages = []
                    for event in events:
                        data = "\n".join(line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:"))
                        if data:
                            messages.append(json.loads(data))
                else:
                    raise McpContractError("unsupported MCP response media type")
                answers = []
                for item in messages:
                    if not isinstance(item, dict) or item.get("jsonrpc") != "2.0":
                        raise McpContractError("invalid MCP JSON-RPC response")
                    if "method" in item:
                        if "id" in item or item["method"] == "notifications/tools/list_changed":
                            self.catalog = {}
                            raise McpContractError("MCP server request or changed tool catalog refused")
                        if item["method"] not in ("notifications/progress", "notifications/message"):
                            raise McpContractError("unexpected MCP notification")
                        continue
                    if type(item.get("id")) is not int or item.get("id") != message.get("id") or "error" in item or "result" not in item:
                        raise McpContractError("MCP response ID or result contract mismatch")
                    answers.append(item["result"])
                if len(answers) != 1:
                    raise McpContractError("MCP requires exactly one matching response")
                return answers[0]
        except JobConflict:
            raise
        except (httpx.HTTPError, ValueError, UnicodeError, KeyError, TypeError):
            raise McpContractError("MCP transport or response validation failed") from None

    def _rpc(self, method, params):
        self.serial += 1
        return self._exchange("POST", {"jsonrpc": "2.0", "id": self.serial, "method": method, "params": params})

    def _initialize(self):
        if self.initialized:
            return
        result = self._rpc("initialize", {"protocolVersion": self.cfg["protocol_version"], "capabilities": {},
                                           "clientInfo": {"name": "qa-buddy", "version": "1"}})
        if not isinstance(result, dict) or result.get("protocolVersion") != self.cfg["protocol_version"] or \
                not isinstance(result.get("serverInfo"), dict) or result["serverInfo"].get("name") != self.cfg["server_name"] or \
                result["serverInfo"].get("version") != self.cfg["server_version"]:
            self.session, self.catalog = None, {}
            raise McpContractError("MCP server identity or negotiated protocol changed")
        if not isinstance(result.get("capabilities"), dict) or not isinstance(result["capabilities"].get("tools"), dict):
            raise McpContractError("MCP server does not advertise tools")
        self._exchange("POST", {"jsonrpc": "2.0", "method": "notifications/initialized"}, notification=True)
        self.initialized = True

    def discover(self):
        """Only contract fingerprints are exposed; descriptions never become model instructions."""
        with self.lock:
            self._headers()  # Detect rotated credentials before reusing a session/catalog.
            self._initialize()
            found, cursor, seen = {}, None, set()
            for _ in range(10):
                page = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
                if not isinstance(page, dict) or not isinstance(page.get("tools"), list):
                    raise McpContractError("invalid MCP tool catalog")
                for tool in page["tools"]:
                    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("inputSchema"), dict):
                        raise McpContractError("invalid MCP tool contract")
                    name = tool["name"]
                    _validator(tool["inputSchema"])
                    if name in found or len(found) >= 256:
                        raise McpContractError("duplicate or excessive MCP tools")
                    found[name] = {"name": name, "sha256": sha256(canon(tool)), "input_schema": tool["inputSchema"],
                                   "read_name": bool(READ_NAME.fullmatch(name))}
                cursor = page.get("nextCursor")
                if not cursor:
                    self.catalog = found
                    return [{k: v for k, v in tool.items() if k != "input_schema"} for tool in found.values()]
                if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in seen:
                    raise McpContractError("invalid or repeated MCP catalog cursor")
                seen.add(cursor)
            raise McpContractError("MCP catalog incomplete at page limit")

    def read(self, operation, params):
        """Only app-typed operations, never a caller/model-selected tool name."""
        with self.lock:
            binding = self.operations.get(operation)
            if not binding:
                raise IntegrationNotAuthorized("MCP operation is not approved")
            local_schema = binding.get("parameters_schema", {"type": "object", "additionalProperties": False})
            if not isinstance(params, dict) or not _validator(local_schema).is_valid(params) or set(params) - set(binding.get("parameters", {})):
                raise McpContractError("typed MCP parameters do not match the reviewed contract")
            arguments = json.loads(canon(binding.get("constants", {})))
            target = arguments
            for name in binding.get("argument_path", []):
                target = target.setdefault(name, {})
            for local, remote in binding.get("parameters", {}).items():
                if local in params:
                    if remote in target:
                        raise McpContractError("MCP parameters cannot replace pinned arguments")
                    target[remote] = params[local]
            if not _validator(binding["arguments_schema"]).is_valid(arguments):
                raise McpContractError("assembled MCP arguments violate the approved scope/operation")
            inner = binding.get("inner_operation")
            if inner:
                value = arguments
                try:
                    for name in inner["path"]:
                        value = value[name]
                except (KeyError, TypeError):
                    raise McpContractError("missing MCP inner operation") from None
                if value != inner["value"] or not READ_NAME.fullmatch(str(value)):
                    raise McpContractError("MCP inner operation is not the approved read")
            self._headers()
            self.discover()
            tool = self.catalog.get(binding["tool"])
            if not tool or tool["sha256"] != binding["tool_sha256"]:
                raise McpContractError("MCP tool contract changed; operator review required")
            if not _validator(tool["input_schema"]).is_valid(arguments):
                raise McpContractError("MCP arguments violate the pinned server schema")
            audit = {"operation": operation, "tool": binding["tool"], "contract": binding["tool_sha256"]}
            if self.gateway:
                reason = self.gateway.check(None, "mcp.read", audit, None)
                if reason:
                    self.gateway.record(None, "mcp.read", audit, "deny", reason)
                    raise IntegrationNotAuthorized("MCP read denied by application policy")
            try:
                result = self._rpc("tools/call", {"name": binding["tool"], "arguments": arguments})
                if not isinstance(result, dict) or result.get("isError"):
                    raise McpContractError("MCP read returned an error")
                payload = result.get("structuredContent")
                if payload is None:
                    content = result.get("content")
                    if not isinstance(content, list) or len(content) != 1 or content[0].get("type") != "text":
                        raise McpContractError("MCP read must return one structured JSON value")
                    payload = json.loads(content[0]["text"])
                if not isinstance(payload, (dict, list)):
                    raise McpContractError("MCP read returned invalid structured data")
                if self.gateway:
                    self.gateway.record(None, "mcp.read", audit, "allow", "pinned read contract", "ok")
                return payload
            except JobConflict:
                raise
            except (IntegrationError, ValueError, KeyError, TypeError) as error:
                if self.gateway:
                    self.gateway.record(None, "mcp.read", audit, "deny", "read failed or response contract rejected")
                if isinstance(error, IntegrationError):
                    raise
                raise McpContractError("MCP read returned malformed structured data") from None
