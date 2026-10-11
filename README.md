# Bluemeteor QA Agent — Milestone A (local, synthetic, replay model)

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

## Layout
| Path | Purpose |
|---|---|
| `qa_agent/interfaces.py` | IssueTrackerProvider, GitProvider (stub only), ModelProvider, TestExecutor, EnvironmentProvider, SkillRegistry, ArtifactStore, ApprovalProvider |
| `qa_agent/runtime.py` | Generic agent flow: story → AC (schema-validated, quote-traced) → plan (schema + skill/oracle validation) → approval → execute → compare → report |
| `qa_agent/gateway.py` | Default-deny tool gateway; every call audited to SQLite (append-only triggers) + JSONL |
| `qa_agent/executor.py` | Playwright executor; per-test context, trace.zip, screenshot, log |
| `qa_agent/skills/` | Domain skills: `pim_import_testing`, `attribute_mapping_validation` |
| `qa_agent/comparator.py` | Deterministic comparison against the oracle manifest |
| `qa_agent/providers/` | `MockJiraProvider`, `ReplayModelProvider`, `GrokProvider` (disabled, fail-closed) |
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
- `GrokProvider` refuses to construct unless `enabled`, `approved`, `approval_ref` and an API key from an approved secret reference are all present. Its `complete()` is not implemented yet.

## Migration notes
**Milestone B (after an approved xAI evaluation account and data policy):** implement `GrokProvider.complete()` against the OpenAI-compatible endpoint, using structured outputs with the schemas in `qa_agent/schemas.py`. Add retry/repair on invalid output, cost caps and a `data_class` guard, then set `"provider": "grok"` in `config/agent.json`. Keep `replay` for CI. Recordings can be captured from approved live runs.
**Milestone C (after separate approvals):** add `JiraRestProvider` (read-only, Cloud or DC) and `BitbucketReadProvider` behind the existing interfaces. Register the verified Playground environment and synthetic tenant in `environments.json`, which means relaxing the loopback-only rule for that one entry. Replace the synthetic oracle with the QA lead's approved manifest. No production entries.
**Deferred production components (contracts preserved):** the SQLite run store and threads map onto a durable workflow engine (Temporal) through `runtime.QAAgent`. `FileArtifactStore` maps onto object storage. The local model-provider switch maps onto a gateway such as LiteLLM or an internal one. Local execution maps onto a sandboxed runner (Docker/gVisor). Chat-page auth maps onto SSO.
