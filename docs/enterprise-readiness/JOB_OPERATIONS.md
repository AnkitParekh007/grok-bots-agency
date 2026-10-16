# Durable work and recovery

Shared deployments require the SQLite queue. A local pilot can opt in with this operator-owned configuration:

```json
"jobs": {
  "enabled": true,
  "planning_workers": 2,
  "execution_workers": 1,
  "lease_s": 60,
  "max_job_s": 1800,
  "max_queued": 100,
  "max_per_tenant": 50,
  "max_per_requester": 20
}
```

Limits apply to queued plus running admissions. Per-kind running counts and execution resource ownership are checked in serialized database transactions across instances using the same database. This is a single-host SQLite implementation, not a supported multi-host cluster or network-filesystem deployment. All instances must run the same reviewed code, configuration and policy. Workers are an on-demand bounded thread pool; planning, execution and payloads are fixed internal operations. Browser/proxy contexts remain isolated per test. One global execution resource conservatively serializes SUT execution, even if the execution worker count is higher.

Run creation and planning admission commit together. Human approval, the state change and execution admission also commit together; a full queue leaves the plan awaiting approval without recording an accepted approval. Queued phases appear as `queued_planning` / `queued_execution` in the API and workspace. The run API exposes job state, attempts, timestamps and errors, excluding caller receipts and payloads.

Web requests carry server-created identity receipts. Sessions are referenced by their hash; raw cookies and bearer secrets are never queued. Workers recheck session revocation, idle/absolute expiry, credential rotation/disablement, tenant/project role and approval identity before dispatch and each gateway call. Background checks do not extend session idle lifetime. OIDC authorization-policy and connection-scope changes invalidate pending work. Execution also checks plan expiry at dispatch. A session expiring while awaiting execution requires a new request/review. Trusted in-process/CLI calls use the operator boundary; it is unavailable through request JSON.

## Restart and interruption

At server startup, recover pending work only after configuring its caller verifier. Unclaimed work with the identical code/configuration fingerprint may be dispatched, subject to its original caller and queue deadline (15 minutes). A changed deployment revision blocks pending work and requires a new request/review. Do not run mixed revisions against one database during a rolling upgrade.

An expired running lease becomes `interrupted`, retains its partial evidence and is never replayed. State/result writes, new gateway calls and proxy/helper traffic are fenced by owner/lease revision. An already dispatched external request cannot be undone by a lease check. Its result must be treated as uncertain if the worker loses authority.

For that reason, an interrupted execution also quarantines its target resource. New executions cannot claim that resource until an operator stops the old worker and reconciles the target. The protected health/metrics views expose the number of quarantined resources. A queue is not evidence that partial target mutations have been compensated.

Before release, stop **all** instances sharing the database, verify their workers and browser children have exited, preserve the interrupted run/audit/evidence, inspect the target and reconcile partial mutations using its approved cleanup procedure. The trusted maintenance API `DurableJobs.reconcile_resource(resource, reviewer, reason, confirmed_stopped=True)` records an operator attestation and releases only that quarantine. It does not retry or change the interrupted job to passed. This maintenance operation is intentionally unavailable as an HTTP endpoint or model tool. After release, restart the same reviewed deployment; only still-authorized unclaimed jobs may run.

Normal shutdown fences active work and waits briefly for workers. A blocking external call may outlive that grace period; terminate the process under the deployment supervisor and reconcile uncertain effects. Never start a replacement execution merely because a thread was abandoned.

## Qualification limits

Offline queue/worker contracts cover cross-connection claims, persisted restart, admission rollback, original-caller revocation, cancellation, lease loss, stale writes and target quarantine. A signed mock IdP plus real Chromium exercise the shared workflow against the synthetic SUT. These do not establish production process supervision, crash/kill recovery under load, network isolation, eight-hour soak performance, organization SUT cleanup, distributed database support or enterprise readiness. Retain the external acceptance gates in the assessment.
