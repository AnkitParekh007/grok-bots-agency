# Enterprise readiness assessment

Assessment date: **2026-10-16**. Code baseline: [`285f9c42f8f6e6f45ed8526f895ad29a5bab6da9`](https://github.com/AnkitParekh007/grok-bots-agency/commit/285f9c42f8f6e6f45ed8526f895ad29a5bab6da9), the merged UI-1 revision. [PR #7](https://github.com/AnkitParekh007/grok-bots-agency/pull/7) merged at 09:27:55 UTC. This is an architecture and implementation assessment, not a penetration-test certificate or regulatory compliance opinion.

## Decision

**NO-GO for enterprise deployment or unattended use against an organization's systems.** Suitable today for supervised, single-process, loopback experiments using synthetic data and registered skills. A real organization pilot remains conditional on the gates in [TEST_PLAN.md](TEST_PLAN.md). Connecting Jira and Bitbucket alone will not make the end-to-end agent work on arbitrary stories.

The strongest implemented controls are deterministic plan validation, independent expected results, exact test-plan approval, requester/approver separation in local auth, default-deny tools, model egress restrictions, quotas, cancellation, and traceable evidence. The UI is substantially more complete than the enterprise operating model and organization workflow.

The most significant demonstrated gaps are:

1. **Browser egress enforcement is incomplete.** In a benign local probe, Chromium followed a redirect and loaded a subresource on a port that the environment policy denied. Checking the initial tool URL does not control all browser/API traffic.
2. **Approval does not bind all execution inputs.** Changing the expected-results manifest after review reached the executor boundary while the approved test-plan hash stayed valid. This probe used a capture stub, not SUT execution.
3. **Organization authorization is too coarse.** Any viewer can read all configured projects through the shared integration identity; approvers can access all runs. There are no user/project memberships or tenant boundaries.
4. **Real-story planning has hard functional gates.** An arbitrary issue without a local oracle manifest fails before planning; real tracker inputs are `unclassified` while configured real-model data classes are synthetic-only. Repository content and custom-field acceptance criteria are not supplied to the planner.
5. **Enterprise identity, MCP, operations and quality qualification are incomplete.** The one recorded native Qwen trial failed semantic validation; replay and protocol tests cannot certify reasoning quality.

## Evidence and limits

| Evidence | Observation | What it proves |
|---|---|---|
| [Merged main CI](https://github.com/AnkitParekh007/grok-bots-agency/actions/runs/38880635554) | Success on the exact baseline; 333 unit tests and 50 integration tests passed | Implemented offline contracts and synthetic browser/API behavior; not organization integration or real-model quality |
| Same CI evaluation | 30 stub runs; seeded-defect recall 18/18; zero false alarms; quality eligibility false | Evaluation harness works on its controlled dataset |
| [B3 trial receipt](../b3/QWEN3_1_7B_TRIAL.json) | Real Ollama/Qwen3 1.7B Q4_K_M inference, 415.76 seconds, 4.899 generated tokens/s; invalid oracle/action pairing; NOT_EVALUABLE | Historical native inference happened; it did not produce an approved executable plan or a model qualification |
| [Assessment probes](PROBE_RESULTS.json) and [reproduction script](reproduce_probes.py) | Seven synthetic observations on baseline | Narrow implementation behaviors described in each receipt; temporary local identities/data only |
| [Evidence manifest](EVIDENCE.json) | Revision, command results, coverage limits, sources and deliverables | Provenance for this report; statuses are not inferred from the UI |

Fresh unit results are recorded in EVIDENCE.json. No new model inference, installation, model download, actual organization access, Jira write, hosted call, deployment, or production test was performed for this assessment. Existing live preview data was not used as proof. Configuration was inspected as code; the presence or absence of real credentials in the operator's environment was not tested.

## Product path: implemented versus missing

| Stage | Implemented | Missing for organization use |
|---|---|---|
| Connect | Direct read-only Jira Cloud/DC and Bitbucket Cloud/DC; process environment token references; exact configuration gates | MCP clients, managed connections, delegated identities, approved live contracts and lifecycle management |
| Ingest | Story summary/description; separate AC/comment/attachment read APIs; bounded repository context reads | Complete, versioned input package; permission filtering per caller; explicit truncation; attachment pipeline |
| Understand | Structured schema, verbatim description quotes, behavior/test/gap validation | Custom-field AC and repository grounding; independently qualified reasoning across organization domains |
| Plan | Two registered domain skills, local fixtures/oracles, bounded repairs, fail-closed policies | Approved organization skill/oracle onboarding and plan-only support when execution capabilities are absent |
| Review | Human identity in local auth; separation; hash and TTL; stale action-catalog checks | Enterprise identity/memberships and approval of the complete immutable execution envelope |
| Execute | Real Chromium and API checks against synthetic import application | SUT authentication, organization selectors/actions, full traffic isolation, durable workers, verified cleanup |
| Report | Results, expected/actual, screenshots, traces, logs, checksums, local hash chain | Evidence lifecycle, central immutable audit, controlled sharing, defect triage/write-back |
| Operate | Single local process; stopped runs marked interrupted | Deployment hardening, HA/DR, queues, telemetry/alerts, support and release controls |

## Prioritized missing pieces

P0 means a blocker before the affected real-data/shared-system pilot. P1 means required before broad enterprise adoption. P2 means product expansion unless the intended pilot explicitly depends on it. These are roadmap priorities, not numerical vulnerability scores. Owners are proposed functions, not assigned people. Every item has acceptance scenarios in [SCENARIOS.csv](SCENARIOS.csv); passing an existing test does not close the broader finding.

### ER-01 — Enterprise identity and secure deployment profile — P0

**Evidence:** [server.py:43](../../qa_agent/server.py#L43), [auth.py:1](../../qa_agent/auth.py#L1), `config/agent.json` auth mode `demo`. Only demo/local auth exists; CLI binds loopback. **Impact:** exposing the demo profile would expose unauthenticated requester/approver capabilities. Local bearer roles do not provide corporate identity lifecycle, MFA or centralized access policy. This is not a claim that the default loopback app is currently internet-exposed.

**Required:** OIDC or an explicitly trusted identity gateway, organizational MFA policy, group-to-role mapping, deprovisioning, service identities, trusted proxy/TLS configuration, and a separate shared-deployment profile that refuses demo auth at startup. App-level authorization must remain in force behind any proxy. **Owner:** IAM/platform. **Close with:** IAM-01–IAM-04, OPS-01; verify user disablement and wrong issuer/audience denials.

### ER-02 — Session lifecycle and authentication abuse — P0

**Evidence:** [server.py:97](../../qa_agent/server.py#L97), [auth.py:51](../../qa_agent/auth.py#L51), probe P02. Cookie is HttpOnly and SameSite Strict, but lacks Secure; logout removes the cookie while the same bearer still works. There is no expiring server session, login throttle or login-attempt audit in these handlers. Static bearer reuse after logout is current design, not proof of token theft.

**Required:** secure session cookies under HTTPS, idle/absolute expiration, centralized revocation/rotation, rate limits, security-event logging, tested CSRF/origin policy and protected caching. Preserve the existing HttpOnly/Strict controls. **Owner:** IAM/security. **Close with:** IAM-05–IAM-09, SEC-09.

### ER-03 — Project and integration authorization — P0

**Evidence:** [server.py:82](../../qa_agent/server.py#L82), [server.py:171](../../qa_agent/server.py#L171), [store.py:1](../../qa_agent/store.py#L1), probe P01. Role checks and run ownership exist, but project/story/repository reads have no membership check. The schema contains no organization/tenant memberships; any approver is globally privileged.

**Required:** explicitly decide dedicated single-organization deployment versus multi-tenancy; enforce organization/project memberships and scoped approval on every resource, connector call, run, artifact, export, model permission and cache key. A shared service token's upstream permissions must never become every user's permissions. **Owner:** security/backend. **Close with:** IAM-10–IAM-12, CON-05, ORG-03, SEC-07.

### ER-04 — MCP integration layer — P1; P0 when choosing MCP

**Evidence:** [interfaces.py:10](../../qa_agent/interfaces.py#L10), [integrations/registry.py](../../qa_agent/integrations/registry.py), requirements and source inventory. The app builds direct REST adapters; there is no MCP client, transport, discovery registry, OAuth callback, MCP connection store or MCP-specific policy enforcement.

**Required:** implement `IssueTrackerProvider`/`GitProvider` over an app-owned connection broker. Normalize MCP and REST results to the same contracts. Pin server identity/protocol/tool schemas; deny arbitrary discovered tools; validate inner operations as well as generic dispatcher names. Support approved remote MCP auth/session lifecycle. Any local stdio server needs a separate trusted installation/process sandbox design. Existing Codex connectors are not automatically product integrations. **Owner:** integration/platform. **Close with:** MCP-01–MCP-12 and connector parity cases.

### ER-05 — Organization skill, fixture and oracle onboarding — P0

**Evidence:** [runtime.py:285](../../qa_agent/runtime.py#L285), [skills](../../qa_agent/skills), probe P04. `_oracles()` requires `<story-key>.expected.json` before planning. Registered skills implement a narrow synthetic import/mapping domain, including `/api/reset`, `/api/import` and fixed selectors.

**Required:** support a clearly non-executable analysis path for unsupported stories; onboard reviewed organization actions, parameter contracts, test fixtures and independently authored expected results before execution. Bind domain/catalog versions to environments. Do not invent oracle values with the same planner being evaluated. **Owner:** agent architecture/domain QA. **Close with:** PLN-07, PLN-08, ORG-06–ORG-09, EXE-01.

### ER-06 — Real-data classification and model authorization — P0

**Evidence:** [runtime.py:317](../../qa_agent/runtime.py#L317), provider `allowed_data_classes` in config. Only fixture tracker stories meeting the synthetic conditions are classified synthetic; real Jira inputs remain unclassified. Default real adapters allow synthetic data. Thus a successful real Jira read does not authorize a model call, including local inference.

**Required:** data-owner-approved classifications, per-source/field provenance, explicit per-model/data-class/region decisions, minimized/redacted inputs, and denial receipts. Preserve rejection of unclassified data; never fix this by globally declaring Jira data synthetic. **Owner:** data owner/security/AI governance. **Close with:** CON-06, MOD-07, ORG-04, SEC-04.

### ER-07 — Grounding and input completeness — P0 for repository/custom-AC workflows

**Evidence:** [runtime.py:310](../../qa_agent/runtime.py#L310), [planning.py:74](../../qa_agent/planning.py#L74), [planning.py:113](../../qa_agent/planning.py#L113), [jira.py:47](../../qa_agent/integrations/jira.py#L47). Repository context is recorded, not passed to planning. Custom-field ACs can be read separately, but messages use summary/description; source-quote validation checks only description. Comments and attachments likewise do not become grounded planner sources.

**Required:** a typed source bundle with immutable source IDs, version/commit, classification and permissions; configured custom AC field and approved contextual sources must enter the prompt/validator consistently. Cite actual file/line/version evidence and show unsupported/unread sources as gaps. **Owner:** agent architecture/integrations. **Close with:** JIR-03–JIR-06, BIT-05–BIT-07, PLN-03, ORG-07.

### ER-08 — Enforce all execution traffic — P0

**Evidence:** [executor.py:39](../../qa_agent/executor.py#L39), [executor.py:46](../../qa_agent/executor.py#L46), [executor.py:100](../../qa_agent/executor.py#L100), probe P06. Tool URLs are checked by the gateway, but there is no browser-context request routing or network sandbox. Actual Chromium loaded a denied-port image and followed a denied-port redirect. API fetch also has no per-hop redirect validation in this code; that path remains a required regression test, not an observed exploit here.

**Required:** browser/API-wide request and redirect controls, service-worker/WebSocket/download handling, DNS/IP/metadata protection and independent worker network policy. Explicitly allow essential SUT/SSO/CDN dependencies; deny everything else and record denials. Navigation checks alone are insufficient. **Owner:** security/execution. **Close with:** SEC-01–SEC-03, EXE-08.

### ER-09 — Immutable reviewed execution envelope — P0

**Evidence:** [runtime.py:329](../../qa_agent/runtime.py#L329), [runtime.py:416](../../qa_agent/runtime.py#L416), [planning.py:54](../../qa_agent/planning.py#L54), probe P03. Hash covers `test_cases`; catalog hash covers registry metadata. Expected-results files are loaded again at execution/report time. Oracle bytes, fixture bytes, actual skill implementation, resolved environment/config, story version and repository commit are not all bound to approval. Existing plan-hash and catalog checks do work within their narrower scope.

**Required:** snapshot and content-hash the complete execution package (plan, source revisions, oracle/fixture bytes, skill artifact/image, resolved target and policy versions, model/prompt/schema identity), display it for review and revalidate immediately before dispatch. Changed inputs require review again. A hash of metadata is not a hash of skill code. **Owner:** runtime/security. **Close with:** APR-06–APR-08, BIT-06, ORG-10.

### ER-10 — Durable orchestration and process ownership — P0 for shared workers

**Evidence:** [runtime.py:80](../../qa_agent/runtime.py#L80), [runtime.py:264](../../qa_agent/runtime.py#L264), [store.py:37](../../qa_agent/store.py#L37), probe P05. Execution lock is an in-process `threading.Lock`; each run creates a daemon thread. Opening a second Store marks the first still-open instance's planning row interrupted. Single-process restart marking is useful; it is not a worker lease protocol.

**Required:** durable queue/state machine, atomic dispatch and leases, fencing, idempotency keys, bounded planning/execution pools, per-environment isolation and safe recovery. A restricted pilot may deliberately remain one process; do not run multiple Uvicorn workers over the same store meanwhile. **Owner:** platform/runtime. **Close with:** OPS-03–OPS-06, EXE-07, ORG-11.

### ER-11 — Secrets and evidence privacy lifecycle — P0 for real data

**Evidence:** [auth.py](../../qa_agent/auth.py), [executor.py:101](../../qa_agent/executor.py#L101), [store.py](../../qa_agent/store.py), [architecture security guide](../ARCHITECTURE_SECURITY.md). Environment secret references and diagnostic redaction exist; SQLite/artifacts are plain local files and traces capture screenshots/DOM. Registered secret-value redaction does not sanitize arbitrary customer content in images or trace payloads.

**Required:** managed secret broker/rotation/revocation, protected filesystem or encrypted managed storage and backups, key ownership, configurable retention/deletion/legal hold, restricted exports and sensitive evidence filtering. Use synthetic markers to test leakage. **Owner:** security/platform/data owner. **Close with:** CON-03, SEC-04–SEC-06, DAT-01–DAT-05.

### ER-12 — Integration transport resilience and resource bounds — P1

**Evidence:** [integrations/http.py:80](../../qa_agent/integrations/http.py#L80), [integrations/http.py:122](../../qa_agent/integrations/http.py#L122), [integrations/http.py:137](../../qa_agent/integrations/http.py#L137), probe P07. The integration HTTP client inherits environment proxy configuration; model transport separately disables it. Response size is rejected after buffering: the 1,024-byte limit probe consumed 3,072 bytes first. Integration Retry-After is parsed as a float and capped by sleep duration, with no persistent long cooldown or HTTP-date handling here.

**Required:** intentional proxy/private-CA policy, bounded streaming/decompression, response/schema/deadline limits, per-account rate coordination, valid Retry-After date/seconds/cooldown handling and jitter/circuit behavior. Respecting a long upstream cooldown may mean deferring/failing a job rather than sleeping a thread. **Owner:** integrations/platform. **Close with:** CON-07–CON-10, JIR-09, DAT-06, OPS-05.

### ER-13 — Real Jira/Bitbucket contract qualification — P1; P0 for the chosen connection

**Evidence:** [jira.py](../../qa_agent/integrations/jira.py), [common.py](../../qa_agent/integrations/common.py), [bitbucket.py](../../qa_agent/integrations/bitbucket.py), [C0 tests](../../tests/test_c0_integrations.py). Cloud enhanced search already uses `/rest/api/3/search/jql`; do not report it as a deprecated-search defect. Pagination is bounded but some results can end silently at `max_pages`; the UI requests only 50 stories without continuation. Cloud attachment endpoints commonly need redirect behavior that the client's deliberate no-redirect policy will reject; verify the selected tenant/operation rather than globally enabling redirects. AC extraction is English-heading/numbered-list oriented and ADF flattening is lossy.

**Required:** live, version-specific certification for token types, scopes, issue security/restricted comments, fields, issue moves/renames, pagination/completeness, attachments, Unicode/rich text, default branches, LFS/submodules and tenant policies. Either secure approved attachment redirects/`redirect=false` where supported or surface unsupported behavior. Preserve GET-only semantics. **Owner:** integrations/QA. **Close with:** JIR-01–JIR-12, BIT-01–BIT-10, CON-01–CON-10.

### ER-14 — Real-model quality, capacity and change qualification — P0

**Evidence:** [B3 receipt](../b3/QWEN3_1_7B_TRIAL.json), [provider tests](../../tests/test_free_providers.py), [evaluation harness](../../scripts/eval_b2.py). Offline protocol coverage exists; one native trial failed semantic validation. Other providers lack certified live quality evidence. No basis exists for claiming that a hosted/free or larger local model will pass.

**Required:** qualification of each exact model/runtime/quantization/prompt/schema/catalog combination with independently reviewed held-out stories, seeded-defect on/off executions, repeatability, latency/memory, usage and operational failure testing. Qualification must be revoked/re-run after material drift. **Owner:** AI evaluation/domain QA. **Close with:** MOD-01–MOD-12, QLT-01–QLT-10.

### ER-15 — Production observability and incident response — P1

**Evidence:** [server.py](../../qa_agent/server.py), [store telemetry](../../qa_agent/store.py). Basic usage/telemetry/audit APIs exist, but no application liveness/readiness contract, metrics exporter, distributed trace propagation, dashboards, alert routing or incident runbooks are delivered. Model connection probes are not service readiness.

**Required:** health and dependency readiness, redacted structured logs, correlation through connector/model/worker, queue/error/resource/privacy alerts, SLO/error-budget dashboards, escalation and incident exercises. **Owner:** SRE/security operations. **Close with:** OPS-01, OPS-02, OPS-09, GOV-05.

### ER-16 — Independently durable audit — P1

**Evidence:** [store audit](../../qa_agent/store.py), [audit verifier](../../qa_agent/audit_verify.py). SQLite triggers/hash chain/checksums detect covered mutations; a filesystem administrator can rebuild the chain. DB and JSONL mirrors are separate writes, not one atomic external audit transaction.

**Required:** reliable audit delivery/outbox, protected external retention/checkpoints/signatures or WORM as appropriate, access-event coverage, time synchronization, SIEM export, gap detection and privileged tamper exercises. **Owner:** security/platform. **Close with:** DAT-07–DAT-09, OPS-08.

### ER-17 — Recovery, backup and migration operations — P1

**Evidence:** [store.py:49](../../qa_agent/store.py#L49) adds columns; interrupted-run handling exists. No versioned deployment/rollback, artifact-consistent backup/restore, retention recovery, RPO/RTO agreement or restore-drill evidence was found in tracked repository assets.

**Required:** consistent DB/artifact/audit backup sets, encrypted restoration, migration/rollback compatibility, disk-full/corruption handling, verified RPO/RTO and partial-side-effect reconciliation. Re-running a failed job must not duplicate mutations. **Owner:** platform/SRE. **Close with:** OPS-06–OPS-08, DAT-04.

### ER-18 — Supply chain and release assurance — P1

**Evidence:** [requirements.txt](../../requirements.txt) uses lower bounds only; [CI](../../.github/workflows/qa-agent-ci.yml) pins action SHAs and runs functional tests, but no tracked dependency lock, SBOM, dependency/security scan, secret scan, signed release, isolated deployment image or release promotion process exists. This inventory is not a finding that a particular installed package is vulnerable.

**Required:** reproducible dependency/browser/image versions, vulnerability and secret scanning with triage, SBOM/provenance, least-privilege build, deployment hardening and tested rollback. Preserve pinned CI actions. **Owner:** DevSecOps. **Close with:** GOV-01–GOV-03, OPS-07.

### ER-19 — Real SUT authentication and cleanup — P0

**Evidence:** [environments.py](../../qa_agent/environments.py) checks test-account and cleanup declarations; [executor.py:100](../../qa_agent/executor.py#L100) creates a fresh unauthenticated browser context, and skills call synthetic reset endpoints. Registry verification does not implement SUT login or execute a general cleanup procedure.

**Required:** approved synthetic accounts, bounded auth flows/storage state, expired-session/MFA handling, environment-specific selectors/API contracts, unique per-run resources, transactional compensation/cleanup verification, mutation ceilings and emergency stop. **Owner:** SUT owner/execution QA. **Close with:** EXE-01–EXE-06, EXE-09–EXE-10, ORG-08–ORG-11.

### ER-20 — Human-reviewed Jira defect workflow — P2

**Evidence:** [interfaces.py:10](../../qa_agent/interfaces.py#L10) contains no write operations; integration HTTP rejects mutations. This is intentional current scope, not broken write support.

**Required if desired:** separate reviewed create/comment/transition capability, destination/project checks, redacted evidence links, duplicate/idempotency handling and organization workflow-field mapping. Keep writes off during the read-only pilot. **Owner:** product/integrations. **Close with:** WRT-01–WRT-06. Becomes a release blocker only if write-back is promised for that release.

### ER-21 — Regression suites and release gates — P2

**Evidence:** current runs are individual plans; [roadmap](../ROADMAP.md) places suite scheduling and release orchestration in future work. **Required:** versioned suites, isolated fixtures, CI identity, approved scheduled execution grants, partial-run aggregation and fail/unknown release-gate semantics. **Owner:** product/platform QA. **Close with:** REG-01–REG-06. Required before claiming release-wide QA automation.

### ER-22 — Workspace accessibility and client qualification — P1

**Evidence:** [UI-1 assessment](../ui-1/WORKSPACE_REDESIGN.md), [UI tests](../../tests/test_ui_workspace.py). Chromium coverage includes role-aware workflows, keyboard/focus, themes and five viewports. No independent assistive-technology audit or Firefox/WebKit/Safari qualification is recorded.

**Required:** supported-browser policy, screen-reader/manual accessibility review, zoom/high-contrast/localization, long-content/error/concurrent-review testing and secure report delivery headers/cache policy. **Owner:** frontend/accessibility QA. **Close with:** UI-01–UI-10, SEC-09.

### ER-23 — Governance, support and commercial adoption — P1

**Evidence:** README explicitly states prototype maturity; no LICENSE or SECURITY.md is tracked at baseline. No approved operational ownership/support contract, AI use register, incident response policy, organization data-handling approval or model/provider license review is supplied. Absence from this repository does not prove the organization has no external policies.

**Required:** named product/data/security owners, agreed scope/SLOs, accepted residual risk, support/escalation, vulnerability disclosure, licensing/procurement decisions and provider/model terms review. Legal/privacy teams determine any applicable obligations; this report does not certify compliance. **Owner:** product/governance/security/legal. **Close with:** GOV-04–GOV-06, ORG-01–ORG-02.

### ER-24 — Untrusted-source and cross-tool adversarial qualification — P1

**Evidence:** [planning.py](../../qa_agent/planning.py) wraps description as untrusted and validates output; injection fixtures exist. Summary, rich content, comments, attachment text, repository files and MCP tool descriptions/results need explicit threat coverage before being incorporated. No current repository-grounding input path means those tests cannot yet prove defenses on that future path.

**Required:** source-separated normalized inputs, hostile-source tests, poison/tool-schema drift checks, output/path/URL validation and explicit refusal/gap behavior. Avoid broad claims that regex or prompt instructions eliminate injection. **Owner:** AI security. **Close with:** SEC-10–SEC-12, MCP-06–MCP-09, PLN-10, QLT-06.

### ER-25 — Application admission control and bounded storage — P1

**Evidence:** [server.py:24](../../qa_agent/server.py#L24) has unconstrained message strings; [runtime.py:264](../../qa_agent/runtime.py#L264) starts a daemon thread per run; list endpoints load local data without pagination; no run/artifact retention limits are delivered. Provider quotas exist, but do not bound all app requests or thread/storage growth.

**Required:** ingress/body/text caps, per-user/project/global admission control, bounded queues/thread pools, paginated lists, run/trace/storage quotas, overload signaling and retention. **Owner:** platform/backend. **Close with:** OPS-04–OPS-05, DAT-06, UI-09, SEC-08.

## Remediation sequence and readiness gates

| Gate | Result now | Deliver before advancing | Accountable function |
|---|---|---|---|
| G0 — Synthetic baseline | Passed within current narrow scope | Preserve CI; address reproduced security issues before expanding access; freeze assessment inputs | Engineering/QA |
| G1 — Connection-only sandbox | Not qualified | Pick REST token or MCP path; organization/project ACLs; secret handling; correct token route/scopes; live read-only sandbox certification | Integrations/security/data owner |
| G2 — Real-story planning only | Blocked | Approved data-class path, normalized AC/context ingestion, non-executable unsupported-story mode, independently reviewed model quality | Agent architecture/AI QA |
| G3 — Supervised nonproduction execution | Blocked | Full input binding/traffic containment, qualified actions/oracles, SUT auth, cleanup, distinct reviewer and scoped execution | Runtime/SUT owner/security |
| G4 — Shared enterprise service | Blocked | IAM, isolation, durable jobs, SLO/DR/evidence/audit/supply-chain operations and sign-off | Platform/security/product |
| G5 — Optional automation/write-back | Not implemented | Separate reviewable capabilities, idempotency, narrow grants, scheduling/release criteria | Product/integrations |

The recommended next milestone is **G1 connection-only sandbox**, with a separate implementation track for ER-08 and ER-09 before any real SUT execution. Choose one supported organization topology initially, then certify the remaining promised topologies. Token REST is the shortest implementation path because adapters already exist; MCP is a valid requested target and must pass its own gate. It is not necessary to enable both routes simultaneously.

## External references checked on the assessment date

These references inform the proposed tests, not a claim of conformance. Provider limits, tool names and account entitlements must be checked again at pilot setup.

- [Atlassian MCP supported tools](https://support.atlassian.com/atlassian-ai-gateway/docs/supported-tools/): documents Jira and Bitbucket Cloud read tools, authentication/scopes, resource discovery and deferred execution. Bitbucket requires an organization-linked workspace. Do not assume hosted Cloud MCP covers Data Center.
- [MCP security guidance](https://modelcontextprotocol.io/docs/2025-11-25/tutorials/security/security_best_practices): informs audience validation, confused-deputy, SSRF and session tests. This is a versioned reference, not a claim that this app implements MCP.
- [Jira enhanced issue search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/): current Cloud pagination/search contract.
- [Jira rate limits](https://developer.atlassian.com/cloud/jira/platform/rate-limiting/): account/auth-specific behavior; API-token traffic is distinguished from OAuth/app quota changes. Test actual headers and applicable limits.
- [Scoped Jira token routing](https://support.atlassian.com/atlassian-cloud/kb/401-unauthorized-error-when-service-account-accesses-jira-or-confluence-api/): scoped service-account tokens use the Atlassian API gateway/cloudId route; tenant-host examples are not universal.
- [Bitbucket Cloud changelog](https://developer.atlassian.com/cloud/bitbucket/changelog/) and [access tokens](https://support.atlassian.com/bitbucket-cloud/docs/access-tokens/): verify current supported token/auth combinations; do not build a new pilot around deprecated app passwords.
- [OWASP ASVS](https://owasp.org/projects/asvs), [OWASP GenAI risks](https://genai.owasp.org/llm-top-10/) and [NIST AI 600-1](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence): reference frameworks for application controls, adversarial testing and AI governance. Full certification/mapping was not performed.
