# B3 local-model evaluation and user checkpoint

[README](../../README.md) · [Local setup](../SETUP_LOCAL.md) · [B2 authorization](../b2/ENABLE_LIVE_EVAL.md)

**Real-model verdict: NOT_EVALUABLE.** No Ollama installation/service or installed local model was found. No real plan, human approval, model-driven execution or model-quality benchmark is claimed. B3 supplies diagnostics and an authenticated smoke/benchmark handoff; installation and download need explicit user approval. No hosted inference, corporate integration, deployment or merge is part of this work.

## Baseline and scope

Inspected 2026-10-15. PRs [#3](https://github.com/AnkitParekh007/grok-bots-agency/pull/3), [#4](https://github.com/AnkitParekh007/grok-bots-agency/pull/4) and [#5](https://github.com/AnkitParekh007/grok-bots-agency/pull/5) are merged. Latest main at branch creation: `d948846f3527bd4e85781e14a0d2320c56257df7`. Branch: `feature/b3-real-local-llm-validation`.

The native Ollama adapter, plan schema, tool policy, independent oracle values and replay fixtures are unchanged. B3 adds one local diagnostics module, a CLI using existing runtime/B2 scoring, offline tests and optional authenticated requester ownership in B2. The wrapper pins the observed model digest for a full-benchmark handoff; B2 refuses resuming a first plan whose configured version or authenticated requester changed. It never creates an approval or substitutes replay when inference fails.

## Observed environment

This is the accessible Windows machine, not a remote Linux/container benchmark. The brief's NVIDIA assumption was checked; **the user selected this machine's CPU/Intel path** after discovery.

| Item | Actual observation |
|---|---|
| OS / Python | Windows 11 Pro, version 10.0.26300, 64-bit; Python 3.13.14 |
| CPU | Intel Core i7-1165G7 at 2.80 GHz; 4 cores / 8 logical processors |
| Display adapter | Intel Iris Xe Graphics |
| Graphics driver | Intel 32.0.101.7088, confirmed through video-controller and signed-display-driver queries |
| NVIDIA / CUDA | `nvidia-smi` absent from PATH and standard NVIDIA locations; no NVIDIA display driver or CUDA toolkit found in the inspected locations. CUDA compatibility and NVIDIA dedicated VRAM are unavailable, not measured as zero. |
| System RAM | 16,905,961,472 usable bytes (about 15.75 GiB); initially about 1.94 GiB free. During parallel browser tests free RAM fell to about 0.4 GiB / 97% load. These are transient observations, not inference measurements. |
| Dedicated VRAM | No NVIDIA measurement. WMI reported `AdapterRAM=2147479552` for Intel; this driver field is **not** a reliable 2 GB dedicated-VRAM claim for integrated/WDDM graphics. |
| C: free space | Initially 106,018,545,664 bytes (about 98.7 GiB); later readings vary with generated test artifacts. |
| Ollama | CLI absent from PATH and checked installation paths; loopback `/api/version` unavailable; no default model store found. No installation or pull performed. |

One automated CIM hardware collection timed out under memory pressure; a subsequent lighter query successfully recorded CPU/video/driver data. The earlier direct queries also confirmed the signed display driver. The diagnostic retains unknown/error fields instead of inferring missing hardware. Parallel browser tests were serialized after RAM exhaustion; the interrupted offline run remains incomplete. Model inference should start only after test processes finish and memory is rechecked. No apps were closed on the user's behalf.

## Candidate decision: estimates, not benchmark results

Current official tags were checked on 2026-10-15. Download sizes below are registry weights, **not total RAM requirements or observed installed digests**. Context/KV cache, runtime and other applications need additional memory; CPU latency/quality remain unknown.

| Candidate | Registry quantization / download | Decision for this machine |
|---|---|---|
| [Qwen3 1.7B](https://ollama.com/library/qwen3:1.7b) | Q4_K_M / about 1.4 GB | Selected **first CPU trial**, pending installation/download approval and RAM headroom; task capability not established. |
| [Qwen3 4B](https://ollama.com/library/qwen3:4b) | Q4_K_M / about 2.5 GB | Possible second trial if the smaller model fails quality and sufficient RAM is available; separate download approval. |
| [Qwen3 8B](https://ollama.com/library/qwen3:8b) | Q4_K_M / about 5.2 GB | Not the first trial given current free RAM and CPU-only latency risk. |
| [Qwen3 14B](https://ollama.com/library/qwen3:14b) | Q4_K_M / about 9.3 GB | Deferred on this loaded 16 GB machine. |
| [gpt-oss:20b](https://ollama.com/library/gpt-oss:20b) | MXFP4 / about 14 GB | Deferred; inadequate current memory headroom for weights plus runtime/context. |
| [Qwen2.5-Coder 3B](https://ollama.com/library/qwen2.5-coder:3b) | Q4_K_M / about 1.9 GB | A coding-model alternative for a separately approved trial; verify its separate model terms. Coding ability alone does not demonstrate faithful QA planning. |

Proposed first-trial settings: exact tag `qwen3:1.7b`, native JSON schema, context **16,384**, output allowance 4,096, timeout 600 seconds. A no-inference preflight measured a conservative full input/schema/catalog plus output ceiling of **11,374**, so the local trial configuration was reduced from 32k to 16k to reduce KV-cache demand. Actual model context support must still be verified after installation. The existing evaluation profile sets a transport retry ceiling of one. These are trial parameters, not a performance promise or relaxed quality threshold. Thinking/temperature/seed are not explicitly set by the existing adapter; server/model defaults apply. Version, template hash, parameters and effective settings are recorded when available. If reasoning consumes the allowance or schema output truncates, record the failure before considering a justified compatibility change.

## Tool behavior and evidence

```powershell
.\.venv\Scripts\python.exe scripts/local_model_b3.py --help
.\.venv\Scripts\python.exe scripts/local_model_b3.py diagnose --out reports/b3/diagnostics
```

`diagnose` performs metadata requests only through the native adapter/gateway: `/api/version`, `/api/tags`, `/api/show`, `/api/ps`. It checks completion/context support, exact installed ID, digest, runtime version, quantization and loaded-memory observations. It calculates the conservative size of the actual PCC-DEMO-5 prompt/catalog/schema without generating text. Discovery neither installs nor downloads. No cloud tags, non-loopback endpoints or real Jira/Bitbucket adapters are accepted by this wrapper.

Artifacts are ignored by Git: timestamped `diagnostics-*.json` and `telemetry-*.json`, `b3_attempt.json`, `review_plan.json`, `b3_summary.json`, and the normal SQLite/audit/report/evidence tree. Diagnostics record Git SHA, working-tree status, relevant source/configuration hashes and effective generation settings; smoke metadata is hash-bound to an audit entry before human review. Unknown telemetry remains null. Windows RAM samples are system-wide, not model-process memory; NVIDIA samples include utilization and memory if available, while Intel dedicated GPU telemetry is unavailable. Raw metadata is local and must be reviewed before sharing.

`plan` generates only PCC-DEMO-5, authenticates the requester from `QA_TOKEN`/the local users file, and returns exit **4** on `awaiting_approval`. It does not execute. CLI handoff output and `review_plan.json` omit benchmark scores/gold so a reviewer can first assess the story, ACs, gaps, actions and hash. `summarize` reads the resulting run and reuses B2 scoring/detection after planning, with full metrics in `b3_summary.json`; a single-story result always stays **SMOKE/NOT_EVALUABLE**. Exit **3** means incomplete/smoke/not-evaluable, not a successful benchmark. Diagnostic exit 0 means metadata readiness only.

## Exact Windows continuation

Run from the repository root. Existing Python dependencies/Chromium must be available as in the [README](../../README.md#quick-start--windows). Commands below requiring Ollama are **next actions after explicit installation/download approval**, not commands already run in B3. Use the official [Windows installer](https://ollama.com/download/windows), reopen PowerShell and keep cloud features disabled. This CPU trial does not assume Intel GPU acceleration.

```powershell
# After explicit installation/download approval only:
ollama --version
ollama pull qwen3:1.7b
ollama list
Invoke-RestMethod 'http://127.0.0.1:11434/api/version'
Invoke-RestMethod 'http://127.0.0.1:11434/api/tags'
```

If the Ollama app has not started its local service, run `ollama serve` in its own terminal. Never start a duplicate listener. Do not switch to a cloud-backed model when CPU inference is slow.

Create a local untracked config (an equivalent `data/b3-local.json` was prepared during this work; rerun only if intentionally replacing it):

```powershell
@'
import json
from pathlib import Path
cfg = json.loads(Path('config/agent.json').read_text(encoding='utf-8'))
cfg['model']['provider'] = 'ollama'
cfg['auth']['mode'] = 'local'
cfg['providers']['ollama'].update(model='qwen3:1.7b', context_window=16384,
    max_completion_tokens=4096, timeout_s=600, max_retries=0)
Path('data').mkdir(exist_ok=True)
Path('data/b3-local.json').write_text(json.dumps(cfg, indent=2), encoding='utf-8')
'@ | .\.venv\Scripts\python.exe -
$env:PYTHONUTF8 = '1'
$env:QA_AGENT_CONFIG = (Resolve-Path 'data/b3-local.json').Path
.\.venv\Scripts\python.exe scripts/local_model_b3.py diagnose --out reports/b3/diagnostics
```

Create identities for actual people; each CLI command prints a token once. A distinct approver must retain/control their own token. Do not paste tokens into chat or logs. These identities have **not** been created by the B3 implementation:

```powershell
.\.venv\Scripts\python.exe -m qa_agent.auth add-user b3_requester --role requester --users-file data/users-b3.json
.\.venv\Scripts\python.exe -m qa_agent.auth add-user b3_approver --role approver --users-file data/users-b3.json
$qaToken = Read-Host 'Your requester token' -AsSecureString
$env:QA_TOKEN = [System.Net.NetworkCredential]::new('', $qaToken).Password
Remove-Variable qaToken
.\.venv\Scripts\python.exe scripts/local_model_b3.py plan --phase defect_on --users-file data/users-b3.json --out reports/b3/qwen3-1_7b/on
```

On a valid real plan this prints its **actual** run ID/hash and review-file path, exits 4 and pauses. If it fails, preserve diagnostics/error/audit evidence; use a fresh attempt directory after fixing the cause. No run ID or generated plan exists for the current missing-runtime checkpoint.

For defect-on execution, Terminal A starts the synthetic application:

```powershell
$env:SUT_DEFECTS = 'all'
.\.venv\Scripts\python.exe -m uvicorn sut.app:app --host 127.0.0.1 --port 8601
```

Terminal B serves the **same** plan data with local auth:

```powershell
$env:PYTHONUTF8 = '1'
$env:QA_AGENT_CONFIG = (Resolve-Path 'data/b3-local.json').Path
$env:QA_DATA_DIR = (Resolve-Path 'reports/b3/qwen3-1_7b/on/data').Path
$env:QA_AUTH_MODE = 'local'
$env:QA_USERS_FILE = (Resolve-Path 'data/users-b3.json').Path
.\.venv\Scripts\python.exe -m qa_agent.server
```

The distinct human approver opens [the workspace](http://127.0.0.1:8600), signs in with their token, reviews the requested run's AC quotations, behavior/gap coverage, skill/action/parameter/oracle mapping and **exact hash**, then explicitly approves or rejects it. They must also verify the intended defect-on phase. Approval executes against the running synthetic SUT; the CLI does not click or approve. If no human is available, stop here with the stored run/hash; plans expire after the configured TTL and need fresh planning when expired.

After completion, open HTML/JSON and screenshot/trace/log links, then collect the actual outcome:

```powershell
.\.venv\Scripts\python.exe scripts/local_model_b3.py summarize --out reports/b3/qwen3-1_7b/on
.\.venv\Scripts\python.exe -m qa_agent.audit_verify --data-dir reports/b3/qwen3-1_7b/on/data
```

For defect-off, stop **both** SUT and agent. Set `$env:SUT_DEFECTS = ''` and restart Terminal A. Generate a **new** plan using the requester token with `plan --phase defect_off --users-file data/users-b3.json --out reports/b3/qwen3-1_7b/off`. Restart Terminal B with `QA_DATA_DIR` pointing to `reports/b3/qwen3-1_7b/off/data`. The human must separately review/approve that new run/hash and verify defect-off mode. Summarize/verify the `off` directory. Never reuse the on-plan approval for off execution.

Inspect actual selected test types: a per-product API action also opens Chromium for evidence, but that is not proof of a browser assertion for an unsupported UI requirement. Keep such requirements as coverage gaps. Counts are model-generated and must not be copied from replay. Compare failed oracle IDs with expected seeded defects, require zero defect-off false alarms and preserve errors/blocked cases.

## Full B2 benchmark after supervised success

The wrapper verifies both completed smoke runs from their original stores and audit bindings, not a self-declared success file. It requires distinct authenticated approval, faithful coverage, seeded-defect detection, no false alarms/errors/policy violations, successful API/browser audit events, report checksum and screenshot/trace/log hashes, and unchanged model digest/runtime/generation sources/settings. It then invokes the existing B2 harness; there is no second scoring framework. Separately authorized evaluation-grant enablement is excluded from the planning-configuration fingerprint, so the subsequent scoped-grant step can proceed without altering a reviewed plan.

```powershell
.\.venv\Scripts\python.exe scripts/local_model_b3.py benchmark --users-file data/users-b3.json --smoke-on reports/b3/qwen3-1_7b/on --smoke-off reports/b3/qwen3-1_7b/off --out reports/b3/qwen3-1_7b/full
```

This first full-harness invocation still pauses for its own authenticated first-plan review. Follow [B2 first-plan review and grant authorization](../b2/ENABLE_LIVE_EVAL.md). A real authenticated approver issues any scoped grant; the wrapper does not. Enable `unattended_eval_approval` in the local config only for a separately authorized grant scope. Preserve story/environment/skill/action/LIVE MODEL limits, expiry/use limits and external sealed-gold custody. Resume the same benchmark command with `--grant <actual-grant-id> --sealed-gold-dir <actual-custody-directory>`. Never use `--self-test-simulated-review` with a real model. A change of requester/digest or failed first plan requires a fresh reviewed handoff.

Without all required stories, repetitions, executions, verified held-out gold and thresholds, the verdict must remain NOT_EVALUABLE/SMOKE or FAIL. Even a complete harness PASS is **not independent quality certification**: the human-authored blind story, independent reviewer and QA-lead agreement/sign-off remain required. Review [contamination limitations](../b2/CONTAMINATION_AND_INDEPENDENCE.md).

Compare reports without further inference:

```powershell
.\.venv\Scripts\python.exe scripts/compare_models.py reports/b3/qwen3-1_7b/full/eval_report.json --out reports/b3/comparison
```

Add other model report paths only after separately approved installations and supervised runs for those exact models. B2 supplies AC precision/recall, coverage, skill/action validity, schema/repairs, gaps, injection/policy behavior, detection/false alarms, latency/usage and repeatability. B3 records native throughput and hardware samples where reported; unknown Intel GPU data stays unknown. No comparison score exists yet for a real local model.

## Failed attempts, tests and outstanding sign-offs

Actual local preflight: service unreachable, no generation performed, exit 3 / NOT_EVALUABLE. One CIM collection timed out under test memory pressure. An offline stub evaluation was interrupted to reduce RAM load and is incomplete; its recordings are not local-model inference. No context/schema/output/skill compatibility failure from a **real model** has been observed, so no speculative relaxation or provider/schema change was made.

| Validation | Result / evidence |
|---|---|
| Full existing suite | **363 passed**, one upstream Starlette/httpx deprecation warning; Windows/Python 3.13.14, 1,345.76 seconds. Invocation started at the merged baseline before B3 tests were added; the optional B3 CLI controls do not change its existing paths. Local receipt: ignored `reports/b3-baseline.xml` and log. |
| Targeted provider/B2/B3 regression run | **83 passed**, one upstream warning; local receipt `reports/b3-targeted.log`. |
| Final B3 contracts | **16 passed**, covering local-only/fixture-only boundaries, metadata-only probes, authenticated requester and distinct-reviewer enforcement, paused planning, contamination rejection, unavailable runtime, audit-bound phase metadata, pinned model versions, missing/tampered evidence and grant handoff. All are offline mocks, not reasoning evidence. |
| Documentation / syntax | Python compilation and CLI help checked; 57 local Markdown references resolved and all 15 PowerShell blocks in README/B3 guide parsed. Embedded Python configuration example parsed. No shared machine-specific defaults were changed. |
| Actual local model | **NOT_EVALUABLE**, metadata preflight could not reach Ollama; no generation, token/TPS/latency measurement, approval or actual model-driven execution. |
| Offline evaluation / CI | The unchanged full GitHub workflow runs unit/integration, complete stub evaluation, comparison and headless replay. Exact-head results and workflow links are reported in the draft PR; they remain harness evidence only. Local browser-heavy attempts were interrupted under memory pressure; no partial local report is called PASS. |

CI remains offline and credential-free; tests use native-protocol mocks and temporary synthetic identities only. No model binaries, raw hardware/configuration files, credentials or machine state are committed.

Outstanding user checkpoints: approve Ollama installation/model download, free enough RAM for a measured CPU trial, supply real requester/approver identities, review the first actual plan and the separate defect-off run, then authorize any larger benchmark/grants. Sealed gold and independent human/QA-lead sign-offs remain outside this implementation's authority.
