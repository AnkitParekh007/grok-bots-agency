# Identity and organization setup

The shared profile uses OIDC authorization code with PKCE and a fixed, operator-approved issuer. It requires an explicit HTTPS workspace origin, encrypted revocable sessions, MFA/authentication-strength claims, and project grants. Its presence does not mean the remaining enterprise assessment gates have passed.

Copy `config/shared.example.json` to an operator-managed configuration outside Git. Fill its null OIDC endpoint/client/approval values from the identity provider's verified configuration. Set `deployment.public_url` to the workspace's HTTPS origin and register that origin plus `/api/oidc/callback` as the exact redirect URI. The application does not fetch endpoints named by tokens or select an issuer from user input. Configure the issuer's signed back-channel logout endpoint to `/api/oidc/backchannel` where supported.

Generate separate Fernet keys for `QA_SESSION_ENCRYPTION_KEY` and `QA_OIDC_FLOW_KEY` using `cryptography.fernet.Fernet.generate_key()`. Deliver them and the optional `QA_OIDC_CLIENT_SECRET` through the deployment's server-side secret mechanism. Keep these values out of chat, browser storage, source control and reports. All workers must share the configured keys and database. Key replacement invalidates affected sessions/flows; plan rotation and sign-in recovery before changing them.

Use the IdP's actual `amr` or `acr` semantics. The example requires `amr` to contain `mfa`; some providers express assurance through `acr` instead. Verify the exact signed claims with the IdP owner. Authentication age is bounded by `max_auth_age_s` (default one hour); this requires `auth_time`. Missing claims fail sign-in. An unsigned group name or a successful login alone grants no project access.

Register projects with `tenant_id` and map approved IdP groups to those exact projects:

```json
{
  "authorization": {
    "mode": "project",
    "group_claim": "groups",
    "group_grants": {
      "qa-requesters": {"tenant_id": "bluemeteor", "projects": {"PROJECT_KEY": "requester"}},
      "qa-reviewers": {"tenant_id": "bluemeteor", "projects": {"PROJECT_KEY": "approver"}}
    },
    "system_auditors": []
  }
}
```

`PROJECT_KEY` is a placeholder, not a discovered project. A group mapped to multiple tenants is rejected. Policy changes force a new sign-in on the next request. Runtime API reads, run creation, approvals, cancellation and artifacts check tenant/project scope. Approval requires a different authenticated human from the requester; service identities cannot review plans. System-wide audit/cost views require a separately named system auditor when project scoping applies.

Local mode remains a dedicated development option. `python -m qa_agent.auth add-user NAME --role requester --tenant TENANT --project PROJECT` creates or rotates a credential. `disable-user NAME` disables it; both operations invalidate existing browser sessions on their next request. Rotation preserves existing tenant/project/kind/expiry unless explicitly changed. Service credentials require a future `--expires-at` UTC timestamp and cannot approve. Local bearer keys are not accepted as shared-profile OIDC credentials.

The owner supplied Jira Cloud site `https://bluemeteor.atlassian.net` and Bitbucket Cloud workspace `bluemeteorapps` (`https://bitbucket.org/bluemeteorapps/workspace/overview/`). Connection discovery must follow authenticated token or MCP setup and explicit resource selection. No Jira project keys, repository slugs, identity provider, organization credentials, SUT authorization, data classification or model-egress approval have been supplied. Keep real integrations disabled until their configuration and resource scope are approved. This guide does not authorize sending organization content to a model.

Deployment still needs independent network controls, managed secrets/storage, backup/restore, and the qualification evidence tracked in `IMPLEMENTATION_STATUS.md`. Test these with a real IdP in an approved sandbox: disabled/removed accounts, group removal, session expiry, replayed callbacks/logout, MFA failures, TLS/proxy behavior and distinct-human approval. Mock-IdP tests establish the code contract only.
