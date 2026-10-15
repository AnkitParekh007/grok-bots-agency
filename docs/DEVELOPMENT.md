# Development and contributions

[README](../README.md) · [Architecture/security](ARCHITECTURE_SECURITY.md) · [Roadmap](ROADMAP.md)

## Environment and conventions

Use Python 3.12+ and the README's virtual environment. CI currently uses Python 3.12 on Ubuntu 24.04. Windows commands below use the virtual environment's interpreter without activation; after activation `python` is equivalent. The frontend is plain HTML/CSS/JavaScript served by FastAPI, with no Node build step. Node is not an application prerequisite.

Follow neighboring Python modules, existing abstract interfaces in `qa_agent/interfaces.py`, JSON configuration/schema/catalog validation and pytest markers in `pytest.ini`. The repository does not mandate a formatter/linter toolchain. Prefer extending a provider, skill or adapter boundary rather than embedding domain behavior in the runtime. Preserve default-deny policy, oracle independence, explicit approvals and recorded model identity. Do not alter gold/replay answers merely to make quality metrics pass.

## Tests and CI equivalents

From the repository root, stop manually started demo services first. Tests and evaluation start their own synthetic/loopback servers. Do not share mutable SUT state between concurrent suites.

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$env:QA_MODEL_PROVIDER = 'replay'
$env:CI = 'true'
Remove-Item Env:QA_AGENT_CONFIG -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m pytest -q -m 'not integration' --junitxml=reports/unit.xml
.\.venv\Scripts\python.exe -m pytest -q -m integration --junitxml=reports/integration.xml
.\.venv\Scripts\python.exe scripts/eval_b2.py --provider stub --self-test-simulated-review --out reports/eval/dev-stub
.\.venv\Scripts\python.exe scripts/compare_models.py reports/eval/dev-stub/eval_report.json --out reports/eval/dev-comparison
$env:QA_DATA_DIR = (Join-Path (Get-Location) 'reports/dev-demo/data')
$env:QA_SCREENSHOTS_DIR = (Join-Path (Get-Location) 'reports/dev-demo/screenshots')
.\.venv\Scripts\python.exe scripts/demo_and_screenshots.py
```

The screenshot/data overrides avoid modifying tracked historical captures. The demo explicitly chooses replay, drives real headless Chromium, approves its **demo-mode** plan and detects planted defects. This automatic demo click is not authenticated real-evaluation approval.

For the CI category-coverage check:

```powershell
foreach ($qaMarker in 'provider','planning','approval','execution','audit','cost','security','eval') {
    $qaCollected = & .\.venv\Scripts\python.exe -m pytest -q --collect-only -m $qaMarker
    if ($LASTEXITCODE -ne 0 -or -not ($qaCollected -match '::')) {
        throw "No tests collected for $qaMarker"
    }
}
```

The [actual GitHub workflow](../.github/workflows/qa-agent-ci.yml) runs on pushes and pull requests with no hosted credentials, installs Chromium with Linux OS dependencies (`python -m playwright install --with-deps chromium`), runs both suites, the offline B2 self-test/comparison, marker coverage and headless demo, then uploads reports. There are no required Node lint/build jobs. Check each command's exit code in your shell/CI; PowerShell does not automatically stop later commands after a native command failure.

Clear session-only settings after tests before an interactive model session:

```powershell
Remove-Item Env:CI, Env:QA_DATA_DIR, Env:QA_SCREENSHOTS_DIR -ErrorAction SilentlyContinue
```

## Local mock services

For a standalone Chat Completions contract simulator:

```powershell
$env:MODEL_API_KEY = 'stub-not-a-real-key-000000'
.\.venv\Scripts\python.exe -m uvicorn qa_agent.devtools.model_stub:app --host 127.0.0.1 --port 8701
```

This is a deliberately fake key and deterministic replay-backed response service, not a real model. Starting it does not switch the agent to it: tests and `--provider stub` build explicit loopback compatible configuration and mock pricing for the contract path. Prefer the evaluation self-test instead of editing real-provider configuration to point at a stub.

In a separate terminal, Jira/Bitbucket-shaped read-only fixture endpoints:

```powershell
.\.venv\Scripts\python.exe -m qa_agent.devtools.integration_mocks 8702
```

The fixture token is literally `synthetic-mock-token-not-a-secret`; the server prints it. It serves synthetic data on loopback and is not a real integration credential. Test harnesses use `MockServer` on a free port and explicit test configuration; production integration authorization still requires HTTPS/allowlists/approval. Use [C0 contract tests](../tests/test_c0_integrations.py) as examples, without relaxing real-host policy. Stop standalone services with Ctrl+C.

## Add a model provider

1. Read `ModelProvider` and the consumed `generate`, discovery, response, usage and cost contracts in [models.py](../qa_agent/providers/models.py). Native Ollama/Gemini adaptations live in [local.py](../qa_agent/providers/local.py); compatible providers share the transport.
2. Implement explicit enablement/approval, exact model ID, data class, credential references, context/output bounds, no redirects/fallback, error/usage normalization, identity/version reporting and quota reservations. Route all inference/metadata HTTP through the gateway. Preserve the monetary ledger for deliberately priced adapters.
3. Register the builder/configuration, registry capability display and evaluation CLI choice as applicable. Start disabled with no real key/endpoint authorization; discovery cannot approve catalog entries.
4. Add offline `httpx.MockTransport` contracts and policy/quota/secret tests using [free provider tests](../tests/test_free_providers.py), [quota tests](../tests/test_free_quota.py) and the [generic compatible regression tests](../tests/test_b1_compatible_provider.py). Test malformed/truncated output, missing usage, 429/retries, unavailable models, redirects, billing mode, contamination and per-run identity.
5. Update the provider table/setup guide, distinguish protocol coverage from reasoning quality, and obtain separate approval before live inference/evaluation.

## Add a QA skill

1. Model a bounded domain action after `qa_agent/skills/pim_import_testing.py` or `attribute_mapping_validation.py`. Define `NAME`, `VERSION`, `DESCRIPTION`, `ACTIONS` mapping names to `(type, function)` and `PARAMS` allowlists.
2. Register the module in `LocalSkillRegistry`. Use the executor's `ToolContext` methods for policy-mediated I/O; never accept arbitrary shell, URLs, expected values or approvals from a plan.
3. Define fixture/oracle references and actual-result fields. Expected values belong in independent oracle manifests, not model output. Unsupported requirements should surface as coverage gaps.
4. Add positive/negative/policy and real synthetic browser/API tests. Verify approval/hash/expiry, fixture path containment and artifact handling. Broaden tool policy only for a concrete authorized action with denial tests.

## Add a Jira or Bitbucket adapter

Implement the read-only `IssueTrackerProvider` or `GitProvider` contract and register it in `qa_agent/integrations/registry.py`. Reuse bounded integration HTTP, exact origin/project/repository allowlists, GET-only semantics, credential references, redaction and pagination/file limits. Add mock pagination, auth, path traversal, redirects, oversized content and denial tests. Start the new configuration disabled. Adapter implementation/testing does not grant real access, classify real data as synthetic or enable model planning on it. Writes require a separate future capability and approval design.

## Add a synthetic story

Add a synthetic Jira fixture under `fixtures/jira/`, CSV inputs under `fixtures/data/`, and independent expected results under `fixtures/oracles/<story>.expected.json`. Follow neighboring fixture formats and registered skill/parameter domains. If the story needs new SUT behavior, implement it in `sut/` with explicit defect toggles and verify both defect-on and defect-off paths.

For offline demonstrations, a hand-authored `fixtures/replay/<story>.json` must remain labeled recorded output and excluded from prompts. For evaluation add gold annotations, required coverage and any injection/untestable requirements under `fixtures/eval/` and register the story in `eval_set.json`. Keep a held-out story's answer out of replay and keep sealed gold outside Git with only its canonical SHA-256 committed. An independent human should author/review blinded cases; see [independence limits](b2/CONTAMINATION_AND_INDEPENDENCE.md).

## Evaluation interpretation

The offline commands above validate schema/scoring/orchestration, not model intelligence. Five primary B2 stories are evaluated with repeated defect-on/off runs; PCC-DEMO-1 is the development demonstration, and PCC-DEMO-7 has externally sealed gold and no stub recording. The scorer reports AC precision/recall from source-quote matching, coverage/gaps, action correctness, first-try schema validity/repairs, injection flagging, planted-defect detection, false alarms, execution errors, repeatability and plan latency. Source matching and near-copy scoring are heuristics; thresholds are proposed.

Usage and latency come from runtime/provider observations, with unknown/missing data kept explicit. Account-scoped quotas and retries bound real calls; a model-free result has no meaningful inference cost/performance comparison. A complete real-model PASS needs all required executions/scoring/approval and genuine reasoning evidence; missing held-out gold or smoke mode cannot support a complete quality claim. See [live procedure](b2/ENABLE_LIVE_EVAL.md) and [independent review](b2/INDEPENDENT_REVIEW_TEMPLATE.md).

## Contributions

Inspect licensing before reusing or redistributing code: no `LICENSE` is present at this revision. A public GitHub repository is not itself an explicit redistribution/modification license. Do not add a license without owner approval.

For changes proposed to the owner, use a focused branch and PR with the problem, resulting behavior and relevant checks. Follow the existing push/pull-request workflow, review CI artifacts and keep tests offline by default. Never commit actual keys/tokens, customer stories, generated `data/`/`reports/` or sealed gold. Historical evidence under `docs/history/` is preserved as superseded provenance; update current guides rather than rewriting historical results. No merge, release or deployment is part of a documentation contribution.

## Workspace frontend

The UI remains vanilla HTML/CSS and native JavaScript modules in `qa_agent/static/`, served from `/static`. `app.js` coordinates API/state/actions; `components.js` renders reusable views; `ui.js` escapes text and implements shared primitives; `workspace.css` holds semantic tokens and responsive styling. No Node build step or runtime package is required. See the [UI-1 audit and workflow map](ui-1/WORKSPACE_REDESIGN.md). Browser interaction coverage is in `tests/test_ui_workspace.py` and runs with the integration suite, using replay and temporary test identities. Set `QA_UI_SCREENSHOTS_DIR` to keep its actual UI captures in a chosen directory.
