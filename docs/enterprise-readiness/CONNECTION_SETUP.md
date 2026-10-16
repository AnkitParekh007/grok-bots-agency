# Token and MCP connection setup

Connections belong to the product, independent of any connectors available inside the coding assistant. Configure them through operator-managed `agent.json` under `connections`. Secret values stay in server-side environment references; the browser APIs accept no token values, server URLs, arbitrary MCP tools, or raw JQL.

The owner supplied Jira Cloud site `https://bluemeteor.atlassian.net` and Bitbucket workspace `bluemeteorapps`. Neither has been connected or used for these tests. The initial [target record](../../config/connections/bluemeteor-cloud.example.json) remains disabled and is not a runnable configuration.

Example disabled token connection to merge into an operator-managed configuration:

```json
{
  "connections": {
    "bluemeteor-bitbucket": {
      "provider": "bitbucket_cloud",
      "route": "token",
      "tenant_id": "bluemeteor",
      "label": "Bluemeteor Bitbucket",
      "display_target": "bluemeteorapps",
      "administrators": [],
      "enabled": false,
      "approved": false,
      "approval_ref": null,
      "discovery_approved": false,
      "discovery": {"owners": ["bluemeteorapps"], "max_pages": 10},
      "http": {
        "base_url": "https://api.bitbucket.org",
        "allowed_hosts": ["api.bitbucket.org"],
        "auth": {"type": "bearer", "token_env": "BLUEMETEOR_BITBUCKET_READ_TOKEN"},
        "timeout_s": 15,
        "max_response_bytes": 2000000,
        "max_retries": 2
      }
    }
  }
}
```

Select `basic` with separate `user_env`/`token_env` when the approved credential type requires it. Do not assume one token type works with every authentication scheme. Jira scoped API tokens may require the `api.atlassian.com/ex/jira/<cloudId>` base rather than the tenant host. Pin the exact approved base and host, including the API base path; never put credentials in URLs. An approved CA bundle can be supplied with `tls_ca_file`. Proxy environment variables are not inherited.

Only a same-tenant authenticated human approver listed in `administrators` can discover or select resources. Use verified stable OIDC identity IDs from `/api/me`, not display names. In Settings, discover the available resources, check the intended repositories/projects, and save the selection. The ten-minute receipt binds the destination configuration, administrator, complete result and previous scope revision. Missing/expired receipts, incomplete discovery, resources outside the result and concurrent edits are rejected. Discovery alone grants no content access. Configuration changes invalidate selections and require fresh review.

Register the intended application projects separately in server configuration. A project references `tracker_connection` and/or `git_connection`, retains its explicit `jira_project`, `repositories`, `tenant_id`, and `environments`, and uses the memberships described in [identity setup](IDENTITY_SETUP.md). Every referenced resource must be in the connection's saved scope. Adapters narrow the shared credential to the project's exact resources; the credential's broader ACL never expands that project. Connection scope revisions are part of the execution envelope, so a scope change invalidates a pending execution approval.

For `route: "mcp"`, replace `http` with an approved `mcp` configuration containing the remote HTTPS `base_url`, host/auth references, `protocol_version`, exact `server_name`/`server_version`, and an operator-reviewed `operations` map. This implementation supports bounded JSON and SSE responses over Streamable HTTP. It does not start local stdio processes. Static server-side Basic/Bearer credentials are supported; interactive MCP OAuth consent/refresh is still pending and must not be claimed as implemented.

Each operation maps an application operation (`resource.discover`, `issue.read`, `issue.search`, `issue.comments`, `issue.attachment`, `repository.read`, `repository.file`, `repository.directory`) to a pinned tool. A binding requires the SHA-256 of the entire discovered tool contract, closed local `parameters_schema` and assembled `arguments_schema`, explicit parameter mapping, and immutable constants such as the approved cloudId. Server metadata is review material; discovery never approves a tool. Changed descriptions/schemas, unknown tools, remote schema references, server requests for roots/sampling/elicitation, and writes/destructive tools are refused.

Generic `executeRead` also requires `inner_operation` with an exact path/value, pinned by the assembled schema and immutable constants. Checking only the outer dispatcher name is insufficient. The server must be trusted and its actual read semantics qualified; tool naming or read-only annotations alone are not evidence that a remote implementation is safe.

MCP collections must carry `complete: true` and the expected collection field. Jira issue results use Jira field objects and must match the requested issue/project. Bitbucket metadata uses `full_name`; file payloads carry `repository`, `path`, `ref`, and `text`; directory payloads carry complete `entries`. Incompatible native payloads need an explicit reviewed normalization contract. This is not a claim of live compatibility with an uninspected hosted tool schema. MCP code search is refused pending an approved source/result contract.

[Atlassian's tool catalog](https://support.atlassian.com/atlassian-ai-gateway/docs/supported-tools/) documents resource discovery and deferred dispatch, including Bitbucket Cloud for organization-linked workspaces. [Token authentication](https://support.atlassian.com/atlassian-ai-gateway/docs/configure-authentication-via-api-token/) distinguishes personal-token Basic auth and service-key Bearer auth; cloudId scope must be pinned explicitly. The transport follows the [MCP Streamable HTTP specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports). Actual Atlassian access, schemas, edition-specific behavior, source ACLs, consent/revocation and quota contracts require an approved sandbox qualification.

These connections authorize bounded reads only. Data classification, model input approval, SUT execution and any future Jira write-back each retain their separate gates. No corporate write has been performed.
