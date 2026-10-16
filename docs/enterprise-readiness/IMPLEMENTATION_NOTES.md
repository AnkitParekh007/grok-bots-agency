# Remediation implementation notes

The implementation ledger covers all 25 findings. These notes explain changes as they are verified; they do not replace the final reassessment or qualify organization/model behavior.

## Execution traffic: ER-08

The executor now creates an owned loopback HTTP/CONNECT proxy for each test context. All browser and API connections use that proxy, including loopback destinations. Every connection is checked against the approved target; DNS results are validated before connecting to a numeric address, preventing a second resolution from changing the destination. Nonpublic addresses need an explicit approved CIDR, except the configured loopback synthetic target; link-local, unspecified and multicast addresses are refused.

Browser request handlers also enforce methods, service workers and downloads are disabled, WebSockets are denied, and Chromium disables QUIC and non-proxied WebRTC UDP. API fetch disables automatic redirects, checks each hop and has a total redirect deadline. Denied traffic makes the test blocked even if the page otherwise appears correct. Existing source URL checks remain in place.

A real local experiment demonstrated that Playwright routes see only the first URL of a redirect chain, including when using `route.fetch(max_redirects=0)` followed by fulfill. Consequently route handlers alone cannot establish destination containment. The proxy validates the later connections as well.

Targeted tests used actual Chromium and two owned loopback services. The forbidden service received zero requests for a denied image and a multi-hop browser redirect. The API redirect case passed after correcting Playwright's use of CONNECT even for plain HTTP. DNS rebinding/private-range tests use injected resolution without external connections.

This is application containment, not an OS security boundary. An enterprise worker deployment still needs independently enforced direct-egress denial, process isolation and qualification of approved TLS/SSO dependencies. These remain explicit ER-08 acceptance work.

## Reviewed execution package: ER-09

New plans snapshot expected-results manifests and fixture bytes. The reviewed envelope fingerprints the full structured planning result, source snapshot, repository context, resolved target, config, policy, schema, model identity, skill catalog, actual action module source and runtime source. Input file size and path bounds apply. Its SHA-256 is the approval token exposed through the existing `plan_hash` API field. This field now denotes the execution envelope; the separate test-case-only digest is recorded inside the manifest.

The workspace displays the target/model and inspectable manifest before review. Approval and execution both verify current inputs against the immutable package. Source/configuration/oracle/fixture/policy drift invalidates approval, and snapshot tampering is rejected. The executor consumes verified fixture bytes captured in memory, so a later file edit cannot change an upload. Expected results similarly come from the verified snapshot. Legacy plans require a new package/review rather than gaining an implicit upgraded approval.

Evaluation and local-model handoff verification now use the same envelope digest, retaining tamper rejection. The JSON report includes the approval format and execution manifest. Baseline historical trial receipts remain unchanged and do not gain qualification from this work.

Targeted package/policy/planning checks: **67 passed**. The package tests include source, target, policy, configuration, expected-result, fixture and snapshot drift, pre-review refusal, and frozen executor input bytes. The latter uses an explicit executor capture stub and is not an actual SUT result.

## Integration transport: ER-12

Integration clients disable environment-proxy inheritance and may use an administrator-configured CA file. Successful bodies are read with bounded streaming; declared oversize/invalid lengths are refused before reading. Compressed responses are refused to avoid unbounded decompression; requests ask for identity encoding. Error bodies are not buffered. Invalid JSON becomes a safe structured integration error.

Retry-After accepts seconds or an HTTP date. A long cooldown defers the request instead of retrying after a shorter sleep, and the cooldown is shared in SQLite when a gateway/store is present. Local clients without a store share an in-process cooldown. Retries remain bounded and GET-only; the full request/stream has a deadline.

Targeted transport checks: **9 passed**, including early stream termination, header/encoding refusal before consumption, no proxy inheritance, shared long cooldown, date parsing, bounded retry and safe malformed JSON. Live account-specific limits, explicit corporate proxy support, broader chaos tests and operational metrics remain qualification work.

## Organization targets

The disabled [onboarding template](../../config/connections/bluemeteor-cloud.example.json) records the owner's Jira Cloud site and Bitbucket Cloud workspace, with secret environment variable names only. Project/repository lists, cloudId, token kind, route, identity provider, classifications and approvals remain explicit unset fields. It is deliberately labeled as an onboarding document, not a runnable agent configuration. No live connection has been made.

## Technical references

- [Playwright request interception](https://playwright.dev/python/docs/network), [BrowserContext](https://playwright.dev/python/docs/api/class-browsercontext) and [API request redirect controls](https://playwright.dev/python/docs/api/class-apirequestcontext): checked against the installed API and local network observations.
- [HTTPX proxy behavior](https://www.python-httpx.org/advanced/proxies/): integration proxy-environment inheritance is now disabled explicitly.

Broad regression suite results and the updated readiness decision will be added after the implementation and qualification audit. Remaining findings are not considered closed by these targeted results.

## Foundation checkpoint regression results

Checkpoint `6644a48` was pushed on `codex/enterprise-readiness`. Its [first CI run](https://github.com/AnkitParekh007/grok-bots-agency/actions/runs/38887700614) passed **355 unit tests**, but failed three of 54 integration cases: evaluation first-plan execution had a changed ephemeral target, and two manual-review CLI cases reconstructed a different configuration. The stub evaluation consequently remained unsuccessful. These are real regressions, not accepted exclusions.

The correction under test persists owned synthetic service ports across evaluation pause/resume, plans the first package only after its target is established, and verifies a per-process service instance header before trusting readiness. The manual-review CLI loads only the configuration snapshot bound to the supplied envelope hash, operates in a mode that cannot plan/dispatch, and retains strict real-target dispatch validation. Expected-result provenance in reports is now read from the verified snapshot as well.

The corrected package and evaluation-handoff selection subsequently passed **15 tests locally** (359.99 seconds), including all three functional regressions from the first CI run. Corrected revision `371feee` then passed [complete CI](https://github.com/AnkitParekh007/grok-bots-agency/actions/runs/38889610311): **356 unit + 54 integration tests**, all 30 offline evaluation runs, 18/18 seeded-defect recall, zero false alarms, and the headless demo. Model-quality eligibility remained false, as required for stub evidence.

A resource-constrained local full integration attempt passed 50 cases and failed four: the same three functional regressions plus an execution wait timeout while suites were competing for memory. The local broad unit attempt was explicitly stopped to serialize resource-heavy tests and is not a passing result. Subsequent results will supersede these failures only after the corrected cases and full CI pass.

The OIDC/PKCE and session-lifecycle cores passed initial isolated checks before integration. They are now being wired into the product with project authorization, secure shared-deployment constraints and browser regression coverage. No real identity provider or organization connection has been used; these findings require additional implementation/qualification before closure.

## Identity and project authorization: ER-01/02/03

The shared profile refuses demo/local authentication and requires an HTTPS public origin, encrypted sessions and an explicit MFA/authentication-strength policy. OIDC uses administrator-pinned endpoints, authorization code with PKCE, browser-bound single-use state, nonce, asymmetric signature/JWKS checks, exact issuer/audience/authorized party, authentication age and verified strength claims. Backchannel logout validates the signed event and prevents replay. Keys/credentials remain server-side references; callbacks never expose raw tokens to the UI.

Local credentials are exchanged for opaque session identifiers rather than being copied into cookies. Session and CSRF values are stored hashed; encrypted session payloads bind the principal to token/row metadata. Idle/absolute expiry, rotation/disablement invalidation, logout revocation, session admission limits and policy-version invalidation are implemented. Direct local API credentials remain a distinct legacy workstation mechanism; disabling/rotating them revokes that access. Shared OIDC does not accept those local bearer keys.

Project roles are checked independently of global roles and connector privileges. Runs carry tenant scope; foreign project/tenant users cannot list, read, cancel, approve or download run data. System-wide telemetry/audit exports require explicit auditor authorization when scope controls apply. Full configuration snapshots are operator-only, preventing a run owner from learning other tenants' configuration through an artifact download. Service identities cannot impersonate human reviewers.

HTTP mutation bodies/concurrent requests are bounded. Cookie mutations require matching CSRF and origin policy, private API responses are not cached, validation errors omit submitted values, and application security headers constrain scripts, framing and content handling. Native OIDC login is included in the workspace. These controls have focused ASGI/mock-IdP coverage; actual IdP policy, corporate source ACLs, operator revocation procedures and supported-browser qualification remain external/system acceptance work.

Focused identity/browser verification passed **37 tests** (297.31 seconds), including actual synthetic SUT execution after distinct mock-IdP requester/reviewer sign-ins and the four existing Chromium workspace workflows. A subsequent hardened selection passed **63 tests** (53.69 seconds): identity/session/HTTP admission, wrong-source response denials, and execution-package drift including stored repository/acceptance/model identity. The package now binds tenant/project/requester metadata as well. These selections overlap and their counts must not be summed as unique coverage. Full checkpoint CI remains pending.

[Identity setup](IDENTITY_SETUP.md) and the intentionally incomplete [shared configuration template](../../config/shared.example.json) document pinned endpoints, group grants, separate encryption keys, expiry, local credential disablement/rotation, and real qualification requirements. Jira responses must match the exact requested key/project even when another project is globally allowlisted. Bitbucket metadata must match the requested repository before subsequent context reads.

Checkpoint `528fbc8` [full CI](https://github.com/AnkitParekh007/grok-bots-agency/actions/runs/38893499338) passed 406 unit cases, all 55 integration cases and the offline evaluation, but failed one legacy unknown-project status assertion and the screenshot demo's string-evaluation polling under the new CSP. The corrected contract expects the deliberate non-disclosing 404 for an unknown project; demo polling now uses Playwright locator assertions without requiring `unsafe-eval`. These corrections need a fresh CI result.

Corrected identity checkpoint `d371fac` passed [complete CI](https://github.com/AnkitParekh007/grok-bots-agency/actions/runs/38894245339): **407 unit and 55 integration tests**, the 30-run offline evaluation, comparison/category checks and the headless demo. Stub model-quality eligibility remains false. A simultaneous local broad unit attempt was explicitly stopped with less than 1 GiB free RAM; it is incomplete, not a passing result.

## Connection broker and remote MCP: ER-03/04/13 (in progress)

The product now has operator-configured token/MCP connections, bounded authenticated resource discovery, a human administrator's explicit scope selection with expiring configuration-bound receipts, optimistic concurrency and revocation. Settings exposes the selection workflow without accepting secret values. Project adapters narrow Jira projects and Bitbucket repositories independently of the upstream credential's broader rights; selected connection revisions join execution approval inputs.

Remote MCP supports approved Streamable HTTP JSON/SSE reads, exact server/protocol identity, session headers and credential rotation detection, pinned tool fingerprints and closed typed argument schemas. Generic read dispatch also binds its inner operation. Discovered instructions do not enter prompts or grant permissions. External schema references, changed tools, unsolicited server requests, writes/destructive tools, unsupported/incomplete results, redirects and scope substitution are refused. Native tool result normalization and interactive MCP OAuth remain explicit qualification/implementation work.

The first focused broker/transport/source selection passed **112 tests** (97.73 seconds), including the 16 initial MCP contracts. Later deadline/revocation/UI changes require their own verification. See [connection setup](CONNECTION_SETUP.md) for the concrete configuration and current boundaries. No organization connection or corporate data access has occurred.

The hardened broker/package/identity selection then passed **68 tests** (91.27 seconds). The final connection/browser selection passed **40 tests** (54.72 seconds), including real Chromium discovery and explicit resource selection, persistence, sign-out followed by another user seeing no connection details, revocation denials, and a live-style SSE stream that stays open after its matching response. The client closes that stream without waiting for EOF. Counts overlap; complete CI for this checkpoint is still required.

Broker checkpoint `20c9ea5` subsequently passed [complete CI](https://github.com/AnkitParekh007/grok-bots-agency/actions/runs/38895675876), including both suites, the offline evaluation and headless demo. This qualifies its offline/synthetic contract behavior only.

## Unsupported stories: ER-05 (partial)

Stories lacking an independent expected-results manifest now produce a separately labeled non-executable source review. Deterministic extraction records source field IDs, hashes, verbatim quotes and observed line references, plus explicit per-criterion capability gaps. No model is called and no execution target is resolved; the report records zero executed tests and no quality eligibility. Unrecognized requirement structure requests human source review. HTML/JSON reports use the existing run/artifact access boundary.

The run ends as `analysis_only`, with no plan hash, execution envelope, approval or test results. Approval/dispatch reject it. The workspace exposes source-review reports and gaps without offering execution approval. This does not author new domain actions or independent expected results; organization onboarding remains required before a newly generated executable plan can be reviewed.

Focused analysis/planning checks passed **43 tests** (31.93 seconds). A real Chromium check passed (32.80 seconds) for the unsupported-story state, absence of review controls, and source-review links. Full checkpoint CI remains pending. A mistyped initial test filename collected no tests and is not counted as verification.

Analysis checkpoint `ea61066` [CI](https://github.com/AnkitParekh007/grok-bots-agency/actions/runs/38896681293) passed its unit stage and 56 of 57 integration cases, but the existing full-workflow UI case failed at project story selection. The new synthetic unsupported-story fixture omitted the status field required by the mock search contract, breaking the project list. The fixture now includes that field; the correction needs full-suite verification.

The corrected fixture and current source-policy changes then passed all **six real Chromium workspace tests** (110.26 seconds), including authenticated execution, accessibility/responsive checks, connection selection and analysis-only presentation.

## Source classification and custom AC grounding: ER-06/07 (partial)

Real source input requires an explicit, unexpired tenant/project data-owner classification with a minimized field list and a separate exact provider/model/endpoint/purpose/declared-region grant. Provider allowlists remain independently required. Default configurations still authorize synthetic data only. Retention/training-use decisions are recorded declarations, not proof of external enforcement. No real organization approval or inference has occurred.

The runtime classifies/minimizes input before native model preparation and creates a typed source bundle. Short-lived internal permits bind actual prompt inputs and source/grant hashes to the run and selected model; providers verify them before every request attempt. Unclassified data, changed scope/content/grants, synthetic relabeling, missing/ambiguous approvals and unsupported sources are denied. Permit values never enter prompts. Source bundles are persisted, fingerprinted in the execution envelope and rechecked before approval/dispatch.

Configured custom acceptance criteria enter both the prompt and validator. Criteria must quote their own authoritative requirement and cite its exact field source ID/hash; description text cannot silently substitute. Observed line locations/content versions are explicit. Upstream Jira version and non-issue sources remain unqualified. [Data approval setup](DATA_APPROVAL_SETUP.md) documents deliberately non-authorizing operator templates.

The focused source/provider/planning/package selection passed **118 tests** (158.51 seconds). A hardened data/package selection passed **33 tests** (35.43 seconds), including the full runtime planning path through a mock native protocol with a fixture answer, custom-source grounding and snapshot-tamper rejection. That test is explicitly stub evidence, not genuine model reasoning. Checkpoint `2fb3f02` then passed [complete CI](https://github.com/AnkitParekh007/grok-bots-agency/actions/runs/38897937089): **458 unit + 57 integration tests**, offline evaluation and demo. Stub model-quality eligibility remains false.

## Governance: ER-23 (partial)

The repository owner explicitly selected **MIT** in this chat on 2026-10-16. Added the standard MIT license under the verified Git author name Ankit Parekh and updated current README/development guidance. Historical baseline assessments retain their accurate observation that no license existed then. Dependency/model/service licensing, data decisions, operating/support owners and final sign-off remain separate acceptance work.

## Durable audit foundation: ER-16 (partial)

Audit sequence allocation and ledger/outbox insertion now commit in a single serialized transaction. The gateway persists a dispatch intent before calling a permitted tool, then records a linked outcome. Missing outcomes remain unknown. Projection delivery fsyncs global/run JSONL records, retries complete-but-unacknowledged lines idempotently, preserves pending records on failure, and applies backlog backpressure. Partial/corrupt projections require explicit recovery; the operator tool preserves prior bytes, verifies the ledger and refuses active runs.

An operator-held Ed25519 checkpoint binds the full canonical prefix, chain head/sequence and an explicit ledger identity. Verification detects altered/shortened prefixes and wrong identities without constructing a runtime. External retention-protected delivery and signing-key custody remain unqualified. Application-state/audit atomicity beyond the audit/outbox transaction remains open.

Focused durability/gateway/audit checks passed **52 tests** (29.68 seconds). Expanded checkpoint/recovery/path checks passed **58 tests** (32.08 seconds). Artifact paths now check path-component containment and single-component run IDs, including rejection of prefix-matching siblings. Full regression remains pending. See [audit operations](AUDIT_OPERATIONS.md).
