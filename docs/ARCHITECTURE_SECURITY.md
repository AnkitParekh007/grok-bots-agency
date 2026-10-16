# Architecture and security boundaries

[README and architecture diagram](../README.md#architecture) · [Local models](SETUP_LOCAL.md) · [Hosted models](SETUP_HOSTED.md)

## Planning and execution

The FastAPI server serves `qa_agent/static/index.html` directly. `QAAgent` owns the run lifecycle, SQLite state and artifact store. The model gateway mediates inference egress; the tool gateway mediates registered execution actions. Models receive story text, system rules and a bounded skill/oracle catalog. Oracle identifiers and field names may be visible, but expected values and evaluation gold are not supplied in prompts.

The model proposes understanding, source-quoted acceptance criteria, testable behaviors, tests and coverage gaps. Schema/catalog validation rejects unknown skills, action/parameter values, arbitrary commands, URLs, expected-result injection and model-supplied approvals. Format failures have a bounded repair path; policy violations are not repaired. A syntactically valid plan can still have poor coverage, so a person reviews it.

Approval binds a SHA-256 execution envelope containing the complete plan/understanding, source/repository snapshots, target, policy/configuration, model, fixtures/oracles and implementation. A default 60-minute TTL applies. Drift requires a new plan/review; execution uses verified snapshot bytes. An owned HTTP/CONNECT proxy validates browser/API connections and DNS destinations. Independent OS/container network isolation remains a deployment requirement. Shared deployments use bounded durable workers with caller revalidation, state/tool/proxy fencing and a database execution resource shared across instances. Expired execution leases quarantine the target until an operator reconciles possible effects; see [job operations](enterprise-readiness/JOB_OPERATIONS.md). The local profile retains its process lock unless the queue is explicitly enabled. Cancelling preserves partial evidence; uncertain work must not be silently replayed.

Stories without independent expected results produce a deterministic `analysis_only` source review with quotes/hashes and capability gaps. This path calls neither a model nor a SUT and cannot be approved for execution. It does not author missing domain actions or expected values.

Reports include model provider/ID and version when reported, plan/results, usage and evidence. Unknown versions stay unknown; mutable tags are not immutable guarantees. The default output root is `data/runs/<run-id>/` with `report.html`, `report.json`, `report.json.sha256`, screenshot/trace/log artifacts and a manifest. Use authenticated workspace report/evidence links when local auth is enabled.

## Local authentication

The default `demo` mode has an unauthenticated approver **label**, not an identity or ownership boundary. Keep it on a single trusted local workstation. It is not enterprise RBAC. For authenticated local testing create separate users from the repository root:

```powershell
.\.venv\Scripts\python.exe -m qa_agent.auth add-user qa_requester --role requester --users-file data/users.json
.\.venv\Scripts\python.exe -m qa_agent.auth add-user qa_approver --role approver --users-file data/users.json
.\.venv\Scripts\python.exe -m qa_agent.auth add-user qa_viewer --role viewer --users-file data/users.json
```

Each command prints a token once. Store it privately; do not record terminal output in logs or screenshots. The file stores PBKDF2-hashed tokens. Reissuing the same user's token rotates that credential. Then start the server:

```powershell
$env:QA_MODEL_PROVIDER = 'replay'
$env:QA_AUTH_MODE = 'local'
$env:QA_USERS_FILE = (Resolve-Path 'data/users.json').Path
.\.venv\Scripts\python.exe -m qa_agent.server
```

Use separate browser profiles/sessions for requester and approver. Browsers exchange a local credential for an opaque revocable session with idle/absolute expiry and CSRF controls. Rotation/disablement invalidates sessions on their next request. Local bearer credentials remain a distinct workstation API mechanism. Viewers cannot create or approve; tenant/project membership and run/evidence ownership apply when configured. Scoped aggregate audit/operations views require system-auditor permission. The server binds loopback by default.

The shared profile requires pinned OIDC/PKCE, HTTPS, encrypted session state, explicit authentication-strength claims and group/project grants. It refuses local/demo credentials. Verified identities with no project grant receive no project access. Service identities cannot review plans. [Identity setup](enterprise-readiness/IDENTITY_SETUP.md) documents the template and actual IdP/lifecycle qualification still required. A shared profile is not an enterprise-readiness certificate.

## Integrations and data classification

`qa_agent/integrations/` implements Jira Cloud/Data Center and Bitbucket Cloud/Data Center REST adapters. Defaults use `mock_jira` and `synthetic_git`; all real entries are disabled/unapproved. Enabling an adapter requires an actual approval reference, HTTPS base URL, exact origin/host allowlist, server-side credential references, and allowed projects/repositories. Bounded pagination, attachment/file limits, origin checks, no redirects and GET-only integration HTTP constrain reads. No Jira create/comment/transition, Git push, clone or pull-request tools exist.

The app-owned [connection broker](enterprise-readiness/CONNECTION_SETUP.md) supports approved token connections and pinned remote MCP reads. Only explicit same-tenant human administrators may discover/select resources. Project adapters narrow the upstream credential to reviewed project/repository scope. Generic MCP read dispatch binds inner operations too; discovered tools never grant themselves permissions. Interactive MCP OAuth and actual hosted payload/ACL contracts remain unqualified.

A successful adapter test does not authorize real access. Source/model decisions are separate: unclassified data is denied, and real sources cannot become synthetic by changing a label. An explicit unexpired owner classification, minimized field list and exact model/data-purpose grant are required, together with the provider's allowlist. [Data approval setup](enterprise-readiness/DATA_APPROVAL_SETUP.md) explains declarations and limits. Approved custom issue ACs enter a typed observed bundle and must cite their own source/hash. Repository context is recorded but not yet used for planning; comments/attachments are likewise separate reads, not silently grounded inputs.

## Environment authorization

`config/environments.json` is independent of model configuration. `synthetic-local` is verified-synthetic on loopback; `SYNTHETIC_SUT_URL` can override its base URL subject to policy. `bluemeteor-dev` and `bluemeteor-playground` have no verified URL/accounts and are non-executable.

A non-loopback environment requires `status:"verified"`, `production:false`, `data_class:"synthetic"`, an HTTPS URL with explicit `allowed_hosts`, a `safety_verification` record (`verified_by`, `verified_at`, `approval_ref`), approved `test_accounts` (each `synthetic:true` with a `credential_env` whose value is present in the server environment), and a `cleanup` record (`procedure_ref`, `steps`). See the actual registry validation in [environments.py](../qa_agent/environments.py). Production names/flags and prohibited host patterns are blocked. An executable environment does not expand skills, approve a model, classify real data as synthetic or grant permission to execute a plan.

## Model egress, credentials and audit

Provider enablement, exact model approval and independent model-host policy remain separate gates. Loopback local engines are the default exception to external HTTPS host approval. Remote local engines need LAN approval, TLS and credentials. Hosted free models need verified account settings, quotas and billing controls. Defaults permit synthetic data only; other inputs require exact source/model-data approvals. Redirects and proxy-environment inheritance are disabled. The UI selects approved new-run models but cannot approve discovered ones.

Credentials are process-environment references read on the server, with registered-value redaction in diagnostics. This is not an encrypted vault. Local configuration, SQLite databases, reports, screenshots and traces are unencrypted files. Environment variables are accessible to trusted processes/administrators; screenshots can capture application data. Protect the workstation and artifact retention, use synthetic accounts/data and inspect exports before sharing.

Audit events use an append-only hash-chained SQLite ledger, pre-dispatch intents, atomic projection outbox and recoverable fsynced JSONL delivery. Verify the default directory with:

```powershell
.\.venv\Scripts\python.exe -m qa_agent.audit_verify --data-dir data
```

Optional operator-held Ed25519 checkpoints bind the full canonical prefix and ledger identity; see [audit operations](enterprise-readiness/AUDIT_OPERATIONS.md). Independent protected custody/delivery has not been configured. A privileged administrator controlling the database and signing key can replace history. Redaction does not establish comprehensive DLP. [Operations](enterprise-readiness/OPERATIONS.md) covers protected metrics, minimized request logs and process-readiness limits.

## Prototype and enterprise limits

These are implemented prototype safeguards with offline/synthetic verification. OIDC and MCP code does not qualify an actual organization. [Encrypted offline recovery](enterprise-readiness/RECOVERY_OPERATIONS.md) verifies a bounded state set and invalidates restored execution/login authority; live storage encryption and production recovery remain unqualified. Managed secret rotation, worker deployment/soak qualification, independent audit/network protection, real model/SUT qualification and operating/support sign-off remain open. Spending controls cannot govern other applications using the same account. The [25-item ledger](enterprise-readiness/IMPLEMENTATION_STATUS.md) tracks remaining implementation and external acceptance gates.
