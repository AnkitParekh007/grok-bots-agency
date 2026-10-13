# Bluemeteor QA Agent — Milestones A + B1 (local, synthetic; no real model reasoning yet)

A small, working, general-purpose QA agent. It reads a Jira-style story, extracts acceptance criteria, proposes a
test plan, waits for **human approval of the exact plan**, runs **real Playwright** browser + API tests against a
**synthetic** application with seeded defects, compares results deterministically against a reviewer-style
expected-results manifest, and produces a downloadable HTML/JSON report with screenshots, traces, logs and an audit trail.

> Milestone A uses a **REPLAYED (mock model)**: the AC extraction and test plan come from
> `fixtures/replay/PCC-DEMO-1.json`, a hand-authored recording. No LLM is called. All of that output is labeled in the UI and the report.
> Execution, comparison and evidence are real.

## Setup (clean checkout)
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium   # or: playwright install chromium
.venv/bin/python -m pytest -q                        # unit + full end-to-end smoke tests
```

## CI / smoke test
GitHub Actions (`.github/workflows/qa-agent-ci.yml`) runs on push and pull_request with `contents: read`, no secrets and actions pinned to commit SHAs. To reproduce the smoke test locally:
```bash
.venv/bin/python -m pytest -q tests/test_e2e_smoke.py && .venv/bin/python scripts/demo_and_screenshots.py
```

## Demo
Interactive:
```bash
scripts/run_demo.sh                 # SUT on 127.0.0.1:8601 (all seeded defects), workspace on 127.0.0.1:8600
SUT_DEFECTS= scripts/run_demo.sh    # same, defects off -> all tests pass
```
Open http://127.0.0.1:8600 and send:
*Read the synthetic supplier import story, generate test cases, execute the approved tests, and give me the results.*
Review the plan, click **Approve & execute**, and watch the steps come in. Then open the report, screenshots, traces (`npx playwright show-trace trace.zip` or trace.playwright.dev) and the audit log.

Headless (drives the same UI with Playwright and refreshes `docs/screenshots/`):
```bash
.venv/bin/python scripts/demo_and_screenshots.py
```
Runs, the SQLite history (`data/qa_agent.db`) and artifacts (`data/runs/<run_id>/`) persist across restarts.
`report.html` is intended to be opened through the workspace (`/api/runs/<id>/report.html`), which resolves its evidence links.

Seeded defects (`SUT_DEFECTS`, comma list or `all`): `inch_factor`, `drop_color`, `no_sku_validation`, `ui_count_off`.
With `all`, the expected result is 3 passed / 5 failed (TC-02, TC-05, TC-06, TC-07, TC-08). With none, it's 8/8 passed.


## Milestone B1 (implemented, local only; no live API calls have been made)

> **No real model reasoning has happened yet.** Plans come either from the **Replay** provider (recordings) or from
> `GrokProvider` pointed at a **local OpenAI-compatible stub** (`qa_agent/devtools/model_stub.py`) that answers from the
> same hand-authored recordings. Labels: `REPLAYED (mock model)` and `STUB ENDPOINT (...; NOT real model reasoning)`.
> Live use (`LIVE MODEL (...)`) is Milestone B2 and needs approvals.

**GrokProvider** (`qa_agent/providers/models.py`)
- Calls `POST {base_url}/chat/completions`, the OpenAI-compatible xAI Chat Completions endpoint. It sends `response_format: {type: json_schema, json_schema: {name, schema, strict: true}}` and `max_completion_tokens`, because the docs mark `max_tokens` as deprecated.
  Sources (checked 2026-10-10): [Structured Outputs](https://docs.x.ai/docs/guides/structured-outputs), [Chat Completions reference](https://docs.x.ai/developers/rest-api-reference/inference/chat-completions), [Rate limits](https://docs.x.ai/developers/rate-limits) (429, back off), [Debugging errors](https://docs.x.ai/docs/debugging).
  xAI recommends the Responses API for new integrations. This build uses Chat Completions for OpenAI compatibility, and moving to Responses is a later change.
- Model name, base URL, timeout, retries, backoff and max tokens are all set in config (`config/agent.json -> providers.grok`). Retries use exponential backoff on 429/5xx/timeouts and honour `Retry-After`, capped. Other 4xx errors are not retried.
- The provider fails closed when any of these is true: `enabled=false`, no API key in the env var `XAI_API_KEY` (read server-side at call time only), pricing not configured, an external host without `approved=true` plus `approval_ref`, a data class not in `allowed_data_classes`, or no audited gateway bound. There is **no automatic fallback** to another provider.
- Egress is checked at the gateway (`model.request`, default deny). Loopback is allowed for the stub. A non-loopback model endpoint must use **https** (checked both in the provider constructor and at the gateway), its host must be listed in `config/policy.json` → `tools.model.request.approved_hosts` (kept separate from the provider `base_url` config) **and** the provider must be approved. Tests show misconfigured calls being denied before anything leaves the process, with a socket guard in place.
- Secrets are redacted from audit, events, errors, logs and reports (`util.redact`, which matches registered env values, `xai-…` and `Bearer …`). A test checks the DB, JSONL, evidence, unzipped traces and every API response for the key after a run in which the endpoint echoed the key back.

**Dynamic planning** (`qa_agent/planning.py`)
- The model receives the story wrapped as untrusted data (`<untrusted_story>`), plus a catalog of registered skill actions with their allowed params, oracle IDs with the ACs/actions they cover and the fields they check (expected values are **withheld**), and the fixture files.
- It returns: understanding, ACs with verbatim quotes, behaviors, positive/negative/edge tests, selected skills, executable specs and coverage gaps. A human approves the plan before anything runs.
- **Policy violations are never repaired.** They are rejected and audited (`model.output_validation` deny). They include: restricted keys (command, url, endpoint, expected, permissions, tool, auto_approve…), unknown skills/actions/oracles, an oracle not approved for those ACs or that action, fabricated ACs or quotes, unsupported or mismatched test types, and params outside the allowlist (file allowlist, SKU pattern, no shell/URL/traversal patterns).
- **Format errors get a bounded repair** (`repair.max_format_repairs`, default 2): invalid JSON, schema mismatch, empty or truncated output (`finish_reason=length`), duplicate IDs, and ACs that are neither tested nor declared as gaps.
- New synthetic stories: `PCC-DEMO-2` (weight units, UNIT_UNKNOWN, ETIM gap), `PCC-DEMO-3` (prompt injection in the story text; a `obey_injection` stub variant tests rejection) and `PCC-DEMO-4` (header-only edge case, size-limit gap). Each has a reviewer-style oracle manifest.

**Cost and usage**
- Each call is stored in SQLite with prompt, cached, completion and reasoning tokens. Cost = usage × configured prices (USD per 1M tokens; reasoning tokens billed as output when `total_tokens` includes them, per the documented examples).
- The per-run cap (`budget.per_run_usd`) and the daily cap (`budget.daily_usd`, UTC day) are checked **before** each call against a conservative estimate. Unknown pricing means the provider won't start. A response with no usage block fails closed and is charged at the estimate.
- Telemetry is at `/api/telemetry`, and per-run usage at `/api/runs/{id}/usage` and in the report. Replay costs $0.
- `pricing` is `null` in the repo on purpose. Enter the prices from your xAI console/models page during B2 enablement. Tests and the B1 demo use **fake placeholder prices**.

**Approvals, auth and audit**
- There are two auth modes (`auth.mode` / `QA_AUTH_MODE`):
  - `demo` (default, for the scripted A demo): the approver name is an **unauthenticated label**, flagged as such in the UI and the report.
  - `local`: PBKDF2-hashed bearer tokens in a users file (`python -m qa_agent.auth add-user NAME --role viewer|requester|approver`), via an Authorization header or an HttpOnly SameSite=Strict cookie from `/api/login`.
- Roles are enforced per endpoint. Approval requires the exact `plan_hash` (a stale hash returns 409 and is audited). Self-approval is forbidden. Duplicate approvals return 409. Rejection stops execution, and so does a plan modified after approval.
- The audit log is hash-chained (`prev_hash` → `entry_hash`) in SQLite and JSONL. Check it with `python -m qa_agent.audit_verify --data-dir data` or `GET /api/audit/verify`; tampering such as an update or a deleted row is detected. Rows from Milestone A predate the chain and are reported as `legacy`.

**Reliability**
- Only one execution runs at a time, because the SUT is shared. A second approval gets 409 and nothing is recorded.
- Runs can be cancelled (`POST /api/runs/{id}/cancel`): remaining tests are marked `not_run` and a partial report is still written.
- After a restart, runs that were in flight are marked `interrupted` with a reason.
- Browser and API calls have timeouts (`execution.*_ms`). Any exception or timeout produces **ERROR**, never PASS, and evidence (screenshot, trace, log) is kept in a `finally` block. The `slow_api` fault in the SUT (`SUT_DEFECTS=slow_api`) exercises this path.


### B1.1 hardening (feature/b1-grok-agent-foundation)
- **Named contracts** (`qa_agent/contracts.py`): Requirement, AcceptanceCriterion, TestStep, TestScenario, TestPlan, SkillSelection, OracleReference, CoverageGap and PlanValidationResult. They are composed into the unchanged wire schema; `planning.evaluate_plan()` returns a PlanValidationResult.
- **Versioned action catalog:** `catalog_version` is a content hash of the skills, actions and parameter specs. It is stored with the plan, and execution is blocked if the catalog changed after planning.
- **Provider:**
  - Bounded backoff jitter (`backoff_jitter_ratio`).
  - A per-attempt correlation ID, sent as `X-Request-ID` and recorded in the audit.
  - Errors normalized into `ModelCallError.kind`: rate_limited, server_error, timeout, transport, auth, not_found, bad_request or invalid_response.
- **Validation:** approval-bypass keys (`skip_approval`, `requires_approval`…), environment and production keys or values (`environment`, `base_url`, `prod`…) and path- or URL-like parameter values are policy violations. Missing step fields and wrong parameter types are format errors. Every rejection is audited.
- **Approvals:**
  - Plans expire after `plan.ttl_minutes` (default 60).
  - In local mode, only the run owner or an approver can cancel a run.
- **Audit v2:**
  - Each row carries a contiguous `seq` and a canonical `payload_hash` (sha256 of sorted-key JSON). `entry_hash = sha256("audit-v2|seq|prev_hash|payload_hash")`.
  - Export to a self-verifying file with `python -m qa_agent.audit_verify export --data-dir data --out audit_export.jsonl` (or `GET /api/audit/export`), and check it with `python -m qa_agent.audit_verify verify-export audit_export.jsonl`.
  - Chains from A (unchained) and B1 (no seq) are still accepted and reported as `legacy` / v1.
- **Cost:** a response without usage stores NULL token counts (usage is never invented) and reserves the conservative estimate against the budget, flagged `priced=0`.


### PR #1 review fixes
- **Budget across retries and concurrent runs:**
  - Before *every* HTTP attempt, including retries, the provider reserves that attempt's worst-case cost: the prompt estimate plus the full `max_completion_tokens`.
  - The reservation is checked atomically against the per-run and daily caps (`Store.reserve_spend` uses `BEGIN IMMEDIATE`, i.e. the SQLite write lock), so concurrent runs and processes sharing the DB cannot jointly overspend.
  - After the attempt, the reservation is settled with actual usage × price. If usage is unknown (HTTP error, timeout, invalid body, missing `usage`), the conservative charge stays, token columns are NULL and `priced=0`.
  - A request denied by policy before it is sent is settled at 0. A retry that would breach a cap is never sent.
- **Egress:**
  - Non-loopback model endpoints must use https. This is checked in the provider constructor and again at the gateway.
  - The approved host list lives in `config/policy.json` (`tools.model.request.approved_hosts`), separate from the provider `base_url` config. `approved_model_hosts` in the provider config is ignored.
- **Run-level access** (`QA_AUTH_MODE=local`):
  - Run list, run detail, usage, reports, audit.jsonl, artifacts and approval are available only to the run's requester (owner) and to approvers. Others get an audited 404.
  - Cancel is owner or approver (other requesters get 403).
  - Cross-run endpoints (`/api/telemetry`, `/api/audit/export`) are approver-only.
  - The `viewer` role can no longer read other users' runs.
- **Demo mode is single-user:** `demo` mode applies no per-run checks and must only be used by one trusted person on localhost.

### B2 prep: evaluation set, quality metrics, live-eval approval pack
- **Evaluation set:** `fixtures/eval/eval_set.json` has five SYNTHETIC stories, PCC-DEMO-2…6.
  - PCC-DEMO-5 covers ETIM class/feature mapping and includes one untestable AC.
  - PCC-DEMO-6 covers required fields and import errors, with prompt injection and one untestable AC.
  - Each story has a gold annotation in `fixtures/eval/gold/`. Oracles and gold are pending QA-lead review.
  - New seeded defects: `etim_alias_ignored` and `name_not_required`.
- **Metrics and PROPOSED thresholds:** `qa_agent/eval_metrics.py`.
- **Harness:** `python scripts/eval_b2.py --provider stub|replay|grok`, which writes `reports/eval/eval_report.{json,md}`.
  - In stub and replay modes it is a harness self-test, **not real model reasoning**.
  - Grok mode requires `--confirm-live-approved` and the approvals in `docs/b2/`. It fails closed with the default config.
- **Approval pack:** `docs/b2/LIVE_EVAL_APPROVAL_REQUEST.md` and `docs/b2/ENABLE_LIVE_EVAL.md`.
- **PR #2 review changes:**
  - **Verdicts fail closed:** PASS / FAIL / NOT_EVALUABLE / SMOKE.
  - **Defect phases:** the harness runs a defect-ON and a defect-OFF phase, and failed or missing runs stay in every denominator.
  - **First-plan handoff:** the harness pauses after the first plan (`PAUSED/AWAITING_FIRST_PLAN_APPROVAL`). It resumes only after an authenticated approver approves that exact plan hash (`python -m qa_agent.eval_authorization review`), and the state survives restarts.
  - **Unattended approval** uses server-side eval grants created by authenticated approvers (`POST /api/eval-grants` or the CLI). Grants are scoped, expiring, use-limited, revocable and audited. The unsigned JSON file path has been removed.
  - **Eval-only profile:** `config/eval_profile.json` sets `max_retries=1` for the harness only; the global default is unchanged.
  - **Held-out story:** PCC-DEMO-7, with gold sealed outside the repo.
  - **Contamination limits:** see `docs/b2/CONTAMINATION_AND_INDEPENDENCE.md`.
  - **Spend cap:** a persistent total cap (`budget.total_usd` per `budget.scope`).

### Trust limits of the local hash chain
The chain catches accidental or partial tampering, such as an edited row, a deleted row, or an out-of-order or duplicated sequence number, **if the verifier holds a previously recorded head hash or export manifest**. Someone with write access to `data/` can rebuild the whole chain consistently, so locally it is *tamper-evident only relative to an externally kept head*. For enterprise use, ship exports or heads to a store the agent host cannot rewrite (WORM/object lock or an external log service) and sign them.

### Demo mode vs authenticated mode
- `demo` (default, used by `scripts/run_demo.sh` and `scripts/demo_and_screenshots.py`): there is no authentication. Anyone who can reach 127.0.0.1:8600 can request and approve, and the approver name is a self-asserted label shown as UNAUTHENTICATED.
- `local` (`QA_AUTH_MODE=local`, used by `scripts/demo_b1.py`): hashed tokens with roles, plan-hash-bound approvals, no self-approval, ownership checks and audited authz denials. It is still not SSO and not suitable beyond a single trusted workstation.

### B1 demo (local auth + local stub; two users)
```bash
.venv/bin/python scripts/demo_b1.py            # PCC-DEMO-3 (prompt-injection story); or pass PCC-DEMO-2 / PCC-DEMO-4
```
This starts the SUT, the stub on `127.0.0.1:8701` and the agent with `QA_AUTH_MODE=local`. It creates the users `ankit` (requester) and `qa_lead` (approver) in `data/users.json` and drives the UI as both users, saving `docs/screenshots/b1_*.png`. The `XAI_API_KEY` it uses is a fake placeholder that is only ever sent to the loopback stub.

### Local-demo vs enterprise-grade controls
| Control | Here (local demo) | Enterprise target |
|---|---|---|
| Identity | demo label or local hashed tokens | Corporate SSO/OIDC, groups → roles |
| Secrets | env var on the server process | Vault/secret manager reference, rotation |
| Audit integrity | hash chain in local SQLite/JSONL (detects tampering, but someone with file access can rewrite the whole chain) | Append-only/WORM store or external log sink with signed checkpoints |
| Egress | in-process gateway + loopback-only env registry | Network egress policy/proxy, sandboxed runners |
| Execution isolation | single local lock, shared SUT | Per-run sandbox + synthetic tenant reset |
| Budget | local SQLite counters | Gateway-level budgets + finance alerts |

### Enabling Milestone B2 (real Grok reasoning), only after approvals
1. Get written approval of the data policy (synthetic data only) and an **organisation-owned** xAI evaluation account. Personal credentials are not acceptable.
2. In `config/agent.json → providers.grok`, set `enabled: true`, `approved: true` and `approval_ref: "<ticket/decision id>"`, confirm `model` and `base_url`, and enter `pricing` from the xAI console. Set `budget` caps.
3. Provide `XAI_API_KEY` from the approved secret store to the server process environment only, then set `model.provider: "grok"` (or `QA_MODEL_PROVIDER=grok`).
4. Run `pytest` (the offline suite still uses replay and the stub), then one supervised PCC-DEMO run and a check that `/api/audit/verify` passes. Compare the live plans against the oracles, and record approved live outputs as new replay fixtures for CI.

## Layout
| Path | Purpose |
|---|---|
| `qa_agent/interfaces.py` | IssueTrackerProvider, GitProvider (stub only), ModelProvider, TestExecutor, EnvironmentProvider, SkillRegistry, ArtifactStore, ApprovalProvider |
| `qa_agent/runtime.py` | Generic agent flow: story → AC (schema-validated, quote-traced) → plan (schema + skill/oracle validation) → approval → execute → compare → report |
| `qa_agent/gateway.py` | Default-deny tool gateway; every call audited to SQLite (append-only triggers) + JSONL |
| `qa_agent/executor.py` | Playwright executor; per-test context, trace.zip, screenshot, log |
| `qa_agent/skills/` | Domain skills: `pim_import_testing`, `attribute_mapping_validation` |
| `qa_agent/comparator.py` | Deterministic comparison against the oracle manifest |
| `qa_agent/providers/` | `MockJiraProvider`, `ReplayModelProvider`, `GrokProvider` (B1: implemented, disabled by config, fail-closed) |
| `qa_agent/planning.py` | Planning schema, prompt (untrusted story wrapping), format-vs-policy validation |
| `qa_agent/auth.py`, `audit_verify.py` | Local auth boundary; audit chain verifier |
| `qa_agent/devtools/model_stub.py` | Local OpenAI-compatible stub (no reasoning) for tests/demos |
| `qa_agent/server.py`, `static/index.html` | Workspace: chat, history, live progress (1 s polling of real events), approval, results, evidence links |
| `sut/app.py` | Synthetic PIM-like app under test (CSV upload UI + API, mapping, UoM conversion, validation, product list) |
| `config/*.json` | Model selection, environment registry, tool policy |
| `fixtures/` | Synthetic story, replay recording, attribute dictionary, supplier CSVs, expected-results manifest |

## Security posture (Milestone A)
- No secrets, no customer data, no organisational systems. Everything binds to 127.0.0.1.
- Environment registry: only `synthetic-local`. Unknown, production or non-loopback environments are refused.
- Tool policy is default-deny (`config/policy.json`). Target-touching tools (`http.request`, `browser.*`) need a recorded human approval **for the exact plan sha256**, a URL inside the environment, an allowed method, and files from `fixtures/data` only.
- Every tool call, allowed or denied, is audited with run id, tool, args sha256, decision, reason, outcome and timestamp.
- Model output is untrusted. It must pass the JSON schema, cite verbatim story text for each AC, and reference only registered skill actions and oracle IDs. Expected values come from the oracle manifest, never from the model or the app code.
- See the B1 section above for the model provider, auth and audit-chain controls.

## Migration notes
**Milestone B:** B1 is done (see above). B2 is enablement only (see the steps above).
**Milestone C (after separate approvals):** add `JiraRestProvider` (read-only, Cloud or DC) and `BitbucketReadProvider` behind the existing interfaces. Register the verified Playground environment and synthetic tenant in `environments.json`, which means relaxing the loopback-only rule for that one entry. Replace the synthetic oracle with the QA lead's approved manifest. No production entries.
**Deferred production components (contracts preserved):** the SQLite run store and threads map onto a durable workflow engine (Temporal) through `runtime.QAAgent`. `FileArtifactStore` maps onto object storage. The local model-provider switch maps onto a gateway such as LiteLLM or an internal one. Local execution maps onto a sandboxed runner (Docker/gVisor). Chat-page auth maps onto SSO.
