# Process health, metrics and incident handling

`GET /healthz` exposes only liveness. `GET /readyz` exposes minimal process readiness and returns 503 on failed database-read, audit-backlog/age or disk-reserve checks. Both are non-cacheable. Readiness is an admission signal, not enterprise qualification. The protected `/api/health` explicitly reports `enterprise_qualified: false`; no model, IdP, organization source or SUT probe is initiated by health polling.

New planning and approval requests are refused while process readiness fails. The tool gateway separately refuses dispatch when audit projections exceed `policy.audit_delivery.max_pending` (default 1,000) or `max_age_s` (default 300 seconds). HTTP body/request limits and model quotas remain separate controls. Active worker recovery and full deployment readiness still require the orchestration/qualification work in the ledger.

`GET /metrics` exports Prometheus aggregate counters, duration histograms, uptime, active-run counts, audit projection backlog/age and disk availability. Labels contain only configured route templates, bounded HTTP methods and status classes. Actual user, tenant, project, story, repository and run IDs never become metric labels. State is per process for HTTP counters; scrape each replica independently and aggregate in the monitoring system.

Scoped deployments require a system-auditor approver for private health/metrics. An optional dedicated scrape credential is read from `QA_METRICS_TOKEN` or `observability.metrics_token_env`; it must contain at least 32 characters. Supply it only through the deployment's server-side secret mechanism and HTTPS Authorization Bearer header. It grants aggregate health/metrics access only, not application roles or approval rights. Rotation/revocation takes effect on the next request. Never put it in a URL or browser storage. Shared-profile scrapers must use the approved HTTPS workspace host.

Every HTTP response receives a generated request ID. Structured `qa_agent.access` logs contain only request ID, configured route template, method, status and elapsed time. They omit raw paths, query strings, bodies, cookies, identities and Authorization values. The repository's `python -m qa_agent.server` entry point disables Uvicorn's raw access log, which would otherwise include OIDC callback query values. If using another ASGI launch command, disable its raw access logging too and validate the deployment's proxy/IdP log policy.

[Example alert rules](../../config/alerts.example.yml) cover readiness, delayed audit delivery, low storage and elevated 5xx rates. They have not been installed or routed to an on-call receiver. Deployment owners must choose actual thresholds, responsibility, notification paths and escalation times, then exercise the alerts in an approved sandbox.

For an incident:

1. Use minimal readiness for traffic admission and protected health details to identify the failed check. Preserve the generated request IDs and minimized logs.
2. Stop new organization work through the deployment's service/traffic controls if admission cannot be trusted. Do not restart uncertain SUT mutations automatically.
3. Preserve the authoritative database, audit outbox, projections, signed external checkpoints and partial run evidence. Do not delete evidence merely to clear storage alerts.
4. For projection corruption or interrupted delivery, follow [audit operations](AUDIT_OPERATIONS.md). Repair requires quiescent planning/execution and does not repair an invalid authoritative ledger.
5. Resolve storage/permissions/transport causes, verify the chain and independent checkpoint, confirm process readiness, and perform a supervised synthetic check before approving new organization work.
6. Record impact, uncertain external effects, timeline, recovery evidence and the designated owner's decision. Full backup/restore, worker fencing, external immutable delivery and real on-call drills remain unqualified acceptance gates.
