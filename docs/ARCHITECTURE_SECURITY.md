# Architecture and security boundaries

[README and architecture diagram](../README.md#architecture) · [Local models](SETUP_LOCAL.md) · [Hosted models](SETUP_HOSTED.md)

## Planning and execution

The FastAPI server serves `qa_agent/static/index.html` directly. `QAAgent` owns the run lifecycle, SQLite state and artifact store. The model gateway mediates inference egress; the tool gateway mediates registered execution actions. Models receive story text, system rules and a bounded skill/oracle catalog. Oracle identifiers and field names may be visible, but expected values and evaluation gold are not supplied in prompts.

The model proposes understanding, source-quoted acceptance criteria, testable behaviors, tests and coverage gaps. Schema/catalog validation rejects unknown skills, action/parameter values, arbitrary commands, URLs, expected-result injection and model-supplied approvals. Format failures have a bounded repair path; policy violations are not repaired. A syntactically valid plan can still have poor coverage, so a person reviews it.

Approval binds to a canonical plan SHA-256, with a default 60-minute TTL. A changed/stale/expired plan needs fresh review. A single execution lock prevents concurrent SUT mutation in this process. Registered skills use `ToolContext` for policy-mediated HTTP/navigation/upload/screenshots and independent oracle comparisons. Browser and API work collects actual evidence even when the plan came from replay. Cancelling retains partial evidence; server restart marks in-flight planning/execution interrupted rather than silently resuming.

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

Use separate browser profiles/sessions to log in as requester and approver. Requesters create their own runs; approvers review/approve, cannot self-approve, and have aggregate visibility. Viewers cannot create or approve; run/evidence ownership still applies. HTTP API clients use bearer tokens; browser login uses an HttpOnly, SameSite=Strict cookie. The server binds loopback by default. Exposing it to a network needs a separate deployment design, TLS and security review; these local tokens are not SSO, MFA or a managed identity service.

## Integrations and data classification

`qa_agent/integrations/` implements Jira Cloud/Data Center and Bitbucket Cloud/Data Center REST adapters. Defaults use `mock_jira` and `synthetic_git`; all real entries are disabled/unapproved. Enabling an adapter requires an actual approval reference, HTTPS base URL, exact origin/host allowlist, server-side credential references, and allowed projects/repositories. Bounded pagination, attachment/file limits, origin checks, no redirects and GET-only integration HTTP constrain reads. No Jira create/comment/transition, Git push, clone or pull-request tools exist.

The UI's project/story/repository selections are server validated. Jira can supply story text, ACs, comments and attachments. Bitbucket can provide repository metadata/files and a bounded context snapshot. **That snapshot is recorded in the run; it is not currently used as model planning input.** Repository-grounded planning is C2 work.

A successful read-only adapter test does not authorize a real account or certify an organization's installation. Real tracker stories are `unclassified`, not automatically synthetic, so default model data-class policy blocks their planning. C1 needs both access approval and an approved data-handling path; simply enabling an adapter is insufficient. Hosted inference in this milestone rejects nonsynthetic data even if a provider allowlist is broadened.

## Environment authorization

`config/environments.json` is independent of model configuration. `synthetic-local` is verified-synthetic on loopback; `SYNTHETIC_SUT_URL` can override its base URL subject to policy. `bluemeteor-dev` and `bluemeteor-playground` have no verified URL/accounts and are non-executable.

A non-loopback environment requires `status:"verified"`, `production:false`, `data_class:"synthetic"`, an HTTPS URL with explicit `allowed_hosts`, a `safety_verification` record (`verified_by`, `verified_at`, `approval_ref`), approved `test_accounts` (each `synthetic:true` with a `credential_env` whose value is present in the server environment), and a `cleanup` record (`procedure_ref`, `steps`). See the actual registry validation in [environments.py](../qa_agent/environments.py). Production names/flags and prohibited host patterns are blocked. An executable environment does not expand skills, approve a model, classify real data as synthetic or grant permission to execute a plan.

## Model egress, credentials and audit

Provider enablement, exact model approval and `config/policy.json` model-host approval are separate gates. Loopback local engines are the default exception to external HTTPS host approval. Remote local engines additionally need explicit LAN approval, TLS and an authenticated gateway credential. Hosted models require verified free-account settings, explicit quotas, synthetic-only inputs and account-side billing controls. Redirects and proxy-environment inheritance are disabled. The UI can select an approved new-run model and probe it; it cannot edit administrator settings or approve a discovered model.

Credentials are process-environment references read on the server, with registered-value redaction in diagnostics. This is not an encrypted vault. Local configuration, SQLite databases, reports, screenshots and traces are unencrypted files. Environment variables are accessible to trusted processes/administrators; screenshots can capture application data. Protect the workstation and artifact retention, use synthetic accounts/data and inspect exports before sharing.

Audit events use SQLite append-only triggers, a hash chain and a JSONL mirror. Verify the default data directory with:

```powershell
.\.venv\Scripts\python.exe -m qa_agent.audit_verify --data-dir data
```

Keep an independently stored chain head/export for stronger detection. A filesystem/database administrator can rebuild a chain; these records are not signatures, WORM storage or proof against a privileged administrator. Redaction reduces accidental disclosure but does not establish comprehensive data-loss prevention.

## Prototype and enterprise limits

The controls are implemented safeguards for the documented local prototype: exact approvals, ownership, default-deny tools/egress, synthetic defaults, production blocking, cancellation, bounded I/O, quotas and evidence. They are not a production security certification. There is no enterprise SSO/MFA, managed secret rotation, encrypted evidence storage, independent audit service, distributed execution lock or centrally enforced account-wide spending across other applications. Hosted privacy and permitted use require current provider/account review. Full organization adoption and approved real environment testing remain future work.
