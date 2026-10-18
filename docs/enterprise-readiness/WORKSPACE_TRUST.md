# Persistent workspace security profile

This change addresses two synthetic observations: authenticated history could be reopened anonymously by switching the server to demo mode; and a saved local-login cookie could be accepted after switching to shared OIDC with the same session encryption key. Offline/API and browser checks cover the implemented boundaries; full CI and actual deployment qualification are recorded separately. No organization data or actual IdP was used in those probes.

## Minimum server profile

Schema v4 adds a singleton `workspace_trust` record. Starting an authenticated workspace raises its minimum profile; starting a shared workspace raises it further. The value cannot decrease or be deleted through supported SQL operations. Profile registration and its audit entry share one transaction, and the server completes registration before admitting HTTP requests.

| Level | Server requirements |
|---|---|
| 0 | Demo, restricted by the existing synthetic-provider checks. |
| 1 | Authenticated local or non-shared OIDC profile; demo is refused. |
| 2 | Shared OIDC profile, with the existing HTTPS, host, session encryption and authentication-strength configuration gates; local/non-shared profiles are refused. |

A new empty demo directory remains supported. Once it has been used by an authenticated server, it cannot return to anonymous service. There is no reset API or configuration bypass. To run a separate anonymous demonstration, choose a new clean data directory and retain the protected directory under its existing access/retention policy. This does not delete evidence or convert private history into synthetic data.

On first opening a directory without a profile record, existing run/audit/connection/review history is conservatively treated as requiring authentication. OIDC evidence requires the shared profile. Legacy inspection reads at most 1,001 metadata rows per inspected table, with an observed one-second/two-million-instruction SQL budget. More than 1,000 rows, interruption or uncertainty requires level 2. It does not load source/report bodies to guess whether they are safe to publish. A large or ambiguous legacy demo history therefore cannot silently become anonymous; use an appropriate authenticated deployment or start a separate clean demonstration.

## Processes already running

Each HTTP request and response chunk rechecks the persisted minimum. An older demo/local process becomes unavailable after another server raises that minimum. The tool gateway and durable-job authorization also recheck it, preventing the older process from starting another policy-mediated tool operation. An already active external operation retains its existing timeout/uncertain-outcome handling; this is not a hard OS interrupt or an external transaction rollback.

The HTTP fence returns a minimized, non-cacheable 503 before response headers. If the profile changes after headers, it aborts the response before sending another unauthorized chunk. Consumers must handle that as an incomplete response. Starlette's path-send extension is disabled for this boundary so file downloads pass through chunk checks. Websocket accept/send messages are similarly fenced; this does not add a websocket product API.

The marker protects QA Buddy's supported server/tool/job paths. It is not a data classifier, filesystem ACL, encrypted volume, signed external attestation or defense against an administrator replacing the database or directly serving files with another program. Direct operator writes to a deliberately public demo directory remain outside this control. Drain older processes when changing identity-provider realms or authorization policy; a monotonic profile level is not distributed configuration/identity-epoch management.

## Browser session boundary

Server-issued sessions bind to the configured authentication mode, shared/local profile, explicit workspace origin, OIDC configuration and project authorization revision. A changed policy requires a new login even when the encryption key is reused. The HTTP principal must also match the server's current authentication mode. Matching-policy restarts retain valid sessions. This binding supplements the existing encrypted/opaque token, row binding, issuer/subject/session references, expiry, idle lifetime and revocation checks.

Existing browser sessions without the new context binding must sign in again after this update. Supported bearer credentials still undergo their normal current-user/role/expiry verification. A context hash is not an additional secret or a replacement for IdP signature, issuer, audience and authentication-strength checks. Actual corporate IdP/session lifecycle and cross-browser qualification remain required.

## Recovery and readiness

Backups retain the profile marker. Restore into a new directory preserves the minimum and continues to invalidate prior sessions, execution authority and uncertain target state as described in [recovery operations](RECOVERY_OPERATIONS.md). Schema-v3 history migrates additively; versions with a lower supported schema refuse the upgraded database. Do not run pre-guard binaries against it.

These changes are partial ER-01/ER-02/ER-09/ER-11/ER-17 controls. They do not qualify shared deployment, an actual IdP, source ACLs, encryption/key custody, immutable audit, retention, native isolation or Supplier Portal execution. The [implementation ledger](IMPLEMENTATION_STATUS.md) remains authoritative for outstanding acceptance work.
