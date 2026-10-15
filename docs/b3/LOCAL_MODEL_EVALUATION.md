# B3 local-model evaluation and user checkpoint

[README](../../README.md) · [Local setup](../SETUP_LOCAL.md) · [B2 authorization](../b2/ENABLE_LIVE_EVAL.md)

**Planning trial: FAIL. Full benchmark: NOT_EVALUABLE.** The approved native Ollama/Qwen3 1.7B CPU trial completed, but its plan was rejected for an unauthorized oracle/action pairing. No valid plan hash, human approval or test execution exists. The failed attempt is preserved; the full multi-story benchmark cannot proceed before a successful supervised smoke test. See the [sanitized machine-readable trial receipt](QWEN3_1_7B_TRIAL.json). No hosted inference, corporate integration, deployment or merge occurred.

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
| Ollama | Initially absent. After user installation, the per-user CLI exists and native loopback `/api/version` reports **0.40.4**; `/api/tags` initially returned an empty model list. Approved `qwen3:1.7b` pull completed with digest verification; 1,359,293,444 installed bytes, Q4_K_M. The tag reports `parameter_size=2.0B` in native metadata; the selected tag remains exactly `qwen3:1.7b`. |

One automated CIM hardware collection timed out under memory pressure; a subsequent lighter query successfully recorded CPU/video/driver data. The earlier direct queries also confirmed the signed display driver. The diagnostic retains unknown/error fields instead of inferring missing hardware. Parallel browser tests were serialized after RAM exhaustion; the interrupted offline run remains incomplete. Model inference should start only after test processes finish and memory is rechecked. No apps were closed on the user's behalf.

## Candidate decision: estimates, not benchmark results

Current official tags were checked on 2026-10-15. Download sizes below are registry weights, **not total RAM requirements or observed installed digests**. Context/KV cache, runtime and other applications need additional memory; CPU latency/quality remain unknown.

| Candidate | Registry quantization / download | Decision for this machine |
|---|---|---|
| [Qwen3 1.7B](https://ollama.com/library/qwen3:1.7b) | Q4_K_M / about 1.4 GB | Selected **first CPU trial**; installation/download approved, runtime/model installed; first CPU trial completed with failed planning validation. Task capability not established. |
| [Qwen3 4B](https://ollama.com/library/qwen3:4b) | Q4_K_M / about 2.5 GB | Possible second trial if the smaller model fails quality and sufficient RAM is available; separate download approval. |
| [Qwen3 8B](https://ollama.com/library/qwen3:8b) | Q4_K_M / about 5.2 GB | Not the first trial given current free RAM and CPU-only latency risk. |
| [Qwen3 14B](https://ollama.com/library/qwen3:14b) | Q4_K_M / about 9.3 GB | Deferred on this loaded 16 GB machine. |
| [gpt-oss:20b](https://ollama.com/library/gpt-oss:20b) | MXFP4 / about 14 GB | Deferred; inadequate current memory headroom for weights plus runtime/context. |
| [Qwen2.5-Coder 3B](https://ollama.com/library/qwen2.5-coder:3b) | Q4_K_M / about 1.9 GB | A coding-model alternative for a separately approved trial; verify its separate model terms. Coding ability alone does not demonstrate faithful QA planning. |

Measured first-trial settings: exact tag `qwen3:1.7b`, native JSON schema, context **16,384**, output allowance 4,096, timeout 600 seconds. A no-inference preflight measured a conservative full input/schema/catalog plus output ceiling of **11,374**, so the local trial configuration was reduced from 32k to 16k to reduce KV-cache demand. Subsequent native metadata verified a 40,960-token model context limit. The existing evaluation profile sets a transport retry ceiling of one. These are trial parameters, not a performance promise or relaxed quality threshold. Thinking/temperature/seed are not explicitly set by the existing adapter; server/model defaults apply. Version, template hash, parameters and effective settings are recorded when available. If reasoning consumes the allowance or schema output truncates, record the failure before considering a justified compatibility change.

## Measured first real trial

The approved pull completed and verified its digest. Native Ollama **0.40.4** served exact tag **`qwen3:1.7b`**, Q4_K_M, installed bytes **1,359,293,444**, digest **`8f68893c685c3ddff2aa3fffce2aa60a30bb2da65ca488b61fff134a4d1730e7`**. Native metadata calls this tag's parameter size `2.0B`; the selected ID was not changed. Model context support is 40,960; the trial used 16,384 with 4,096 output tokens and 600-second timeout. Model defaults reported temperature 0.6, top_k 20, top_p 0.95, repeat_penalty 1. Thinking/seed were not explicitly set. The result receipt records the template/settings/prompt/source hashes.

An actual authenticated **automation requester**, `b3_agent_requester`, initiated PCC-DEMO-5 in local auth mode. Its token was held in the planning process, not printed or committed. No human approver was created. The run used the native adapter and synthetic story/catalog, with no replay response or gold answer inserted into the prompt. Tested source commit was `b788c797749ae6c9f427cd3e9a55eaa73b832e24`; the working tree had documentation edits only. Relevant runtime/source hashes were recorded.

| Measurement | Observed result |
|---|---|
| Run / completion time | `run-da6b623233`, 2026-10-15 16:57:29 UTC |
| Native calls / finish | One real `/api/chat` completion; normal `stop`; no transport retry or format repair |
| Tokens | 1,019 prompt + 1,896 generated = 2,915 total; separate reasoning breakdown unavailable (the existing normalized ledger's zero is not a measured reasoning count) |
| Latency / generation speed | Planning 415.76 s; native-adapter call 414,318.777 ms; 4.899 generated tokens/s |
| Context / output / timeout | Conservative preflight 11,374 fits 16,384; normal stop below the 4,096 output and 600-second limits. No observed truncation/context/timeout failure. |
| Loaded model / GPU | `/api/ps`: 3,384,016,895 loaded bytes, `size_vram=0`, context 16,384. CPU inference; no Intel acceleration claim. |
| System RAM | 177 best-effort samples; peak **99%** load, lowest available **49,176,576 bytes**. This is system-wide pressure, not model-process RSS. The model was unloaded after the trial. |
| Validation | JSON parsing/schema checks passed before semantic validation rejected `TC-1`: `ORC5-CLASS-5001` is not approved for `api_import`. One policy violation; no valid plan/hash. |
| Audit / approval / execution | Audit chains verify; zero human approvals; zero API/browser test execution; no screenshots/traces or execution report |
| Quality / full benchmark | Unavailable after failed validation. Defect recall, false alarms, coverage and repeatability are not measured. **NOT_EVALUABLE**, never PASS. |

The exact prompt catalog lists only `api_product_after_import` for `ORC5-CLASS-5001`, and the system instruction requires the oracle's allowed actions to include the selected action. The observed failure is therefore an invalid model-generated pairing despite the provided mapping. JSON-schema support alone did not ensure semantic correctness. This trial ran under memory pressure; its separate contribution to latency or the mapping error is unknown, and one attempt does not establish a model-wide failure rate.

The validator correctly treated this as a non-repairable policy violation. No security check, action/oracle allowance or expected value was relaxed, and the invalid plan was not manually rewritten into a passing one. The existing runtime retains the rejected output's SHA-256 and denial reason rather than the raw invalid candidate. Consequently the review artifact has `planning:null`, `plan_hash:null`, `validation_status:failed`; detailed scores or the full rejected candidate cannot be reconstructed. The summary's `real_reasoning_evidence:false` means there is no validated plan evidence; **an actual native completion did occur**.

Local evidence: ignored `reports/b3/qwen3-1_7b/on/{b3_attempt.json,b3_summary.json,review_plan.json}`, the named telemetry JSON, `data/qa_agent.db`, `data/audit.jsonl`, and `reports/b3/qwen3-1_7b/loaded-model.json`. The committed [receipt](QWEN3_1_7B_TRIAL.json) records checksums and measured values without tokens, model binaries, raw machine configuration or confidential data.

Further inference needs materially more free RAM than this trial had. A larger candidate remains a separate download decision; none is installed/authorized beyond the selected 1.7B tag. First obtain a valid new plan, then a distinct authenticated human must review it. The defect-off execution and full B2 benchmark remain gated on separately approved successful smoke runs.

## Tool behavior and evidence

```powershell
.\.venv\Scripts\python.exe scripts/local_model_b3.py --help
.\.venv\Scripts\python.exe scripts/local_model_b3.py diagnose --out reports/b3/diagnostics
```

`diagnose` performs metadata requests only through the native adapter/gateway: `/api/version`, `/api/tags`, `/api/show`, `/api/ps`. It checks completion/context support, exact installed ID, digest, runtime version, quantization and loaded-memory observations. It calculates the conservative size of the actual PCC-DEMO-5 prompt/catalog/schema without generating text. Discovery neither installs nor downloads. No cloud tags, non-loopback endpoints or real Jira/Bitbucket adapters are accepted by this wrapper.

Artifacts are ignored by Git: timestamped `diagnostics-*.json` and `telemetry-*.json`, `b3_attempt.json`, `review_plan.json`, `b3_summary.json`, and the normal SQLite/audit/report/evidence tree. Diagnostics record Git SHA, working-tree status, relevant source/configuration hashes and effective generation settings; smoke metadata is hash-bound to an audit entry before human review. Unknown telemetry remains null. Windows RAM samples are system-wide, not model-process memory; NVIDIA samples include utilization and memory if available, while Intel dedicated GPU telemetry is unavailable. Raw metadata is local and must be reviewed before sharing.

`plan` generates only PCC-DEMO-5, authenticates the requester from `QA_TOKEN`/the local users file, and returns exit **4** on `awaiting_approval`. It does not execute. CLI handoff output and `review_plan.json` omit benchmark scores/gold so a reviewer can first assess the story, ACs, gaps, actions and hash. `summarize` reads the resulting run and reuses B2 scoring/detection after planning, with full metrics in `b3_summary.json`; a single-story result always stays **SMOKE/NOT_EVALUABLE**. Exit **3** means incomplete/smoke/not-evaluable, not a successful benchmark. Diagnostic exit 0 means metadata readiness only.

## Exact Windows continuation

Run from the repository root. Existing Python dependencies/Chromium must be available as in the [README](../../README.md#quick-start--windows). Installation and this exact model download are now authorized. The user completed manual installation; the local runtime and approved pull completed verification. The commands below also document reproduction. Keep cloud features disabled. This CPU trial does not assume Intel GPU acceleration.

The verified official [Ollama v0.40.4 release](https://github.com/ollama/ollama/releases/tag/v0.40.4) installer is in ignored `reports/b3-installer/OllamaSetup.exe` (1,581,967,176 bytes). Its SHA-256 matches the release asset: `45fcd47d70dd047ab95736802e71a7497ef1a473a50470471fc2fb0eb90badde`. Windows Authenticode status is **Valid**, publisher **Ollama Inc.** Automatic approval review rejected the attempted installer launch with only “blocked by policy”; no alternative automation was used to bypass it. The user ran the verified file manually and reported installation complete. A subsequent automatic service-start attempt was also rejected before execution; read-only probes then confirmed the user-started service was already available, so no alternate launch was attempted. Local receipts are `verification.json` and `launch-blocked.json` in the same ignored directory. `~/.ollama/server.json` was prepared with `disable_ollama_cloud:true`, preserving any existing settings.

```powershell
# Reproduction after the already approved installation:
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

The first trial uses an authenticated automation requester named `b3_agent_requester`, created in ignored `data/users-b3.json`; its token was held only in the planning process and never printed. No human approver identity or approval was created. For subsequent manual runs, create a requester and a distinct human approver; each CLI command prints a token once. The human must retain/control their own approver token. Do not paste tokens into chat or logs:

```powershell
.\.venv\Scripts\python.exe -m qa_agent.auth add-user b3_requester --role requester --users-file data/users-b3.json
.\.venv\Scripts\python.exe -m qa_agent.auth add-user b3_approver --role approver --users-file data/users-b3.json
$qaToken = Read-Host 'Your requester token' -AsSecureString
$env:QA_TOKEN = [System.Net.NetworkCredential]::new('', $qaToken).Password
Remove-Variable qaToken
.\.venv\Scripts\python.exe scripts/local_model_b3.py plan --phase defect_on --users-file data/users-b3.json --out reports/b3/qwen3-1_7b/on-next
```

On a valid real plan this prints its **actual** run ID/hash and review-file path, exits 4 and pauses. If it fails, preserve diagnostics/error/audit evidence; use a fresh attempt directory after fixing the cause. The first real attempt, `run-da6b623233`, failed policy validation with a null plan hash. It cannot be approved or executed; do not use its directory for a new attempt. A future valid attempt must use a fresh output directory.

For defect-on execution, Terminal A starts the synthetic application:

```powershell
$env:SUT_DEFECTS = 'all'
.\.venv\Scripts\python.exe -m uvicorn sut.app:app --host 127.0.0.1 --port 8601
```

Terminal B serves the **same** plan data with local auth:

```powershell
$env:PYTHONUTF8 = '1'
$env:QA_AGENT_CONFIG = (Resolve-Path 'data/b3-local.json').Path
$env:QA_DATA_DIR = (Resolve-Path 'reports/b3/qwen3-1_7b/on-next/data').Path
$env:QA_AUTH_MODE = 'local'
$env:QA_USERS_FILE = (Resolve-Path 'data/users-b3.json').Path
.\.venv\Scripts\python.exe -m qa_agent.server
```

The distinct human approver opens [the workspace](http://127.0.0.1:8600), signs in with their token, reviews the requested run's AC quotations, behavior/gap coverage, skill/action/parameter/oracle mapping and **exact hash**, then explicitly approves or rejects it. They must also verify the intended defect-on phase. Approval executes against the running synthetic SUT; the CLI does not click or approve. If no human is available, stop here with the stored run/hash; plans expire after the configured TTL and need fresh planning when expired.

After completion, open HTML/JSON and screenshot/trace/log links, then collect the actual outcome:

```powershell
.\.venv\Scripts\python.exe scripts/local_model_b3.py summarize --out reports/b3/qwen3-1_7b/on-next
.\.venv\Scripts\python.exe -m qa_agent.audit_verify --data-dir reports/b3/qwen3-1_7b/on-next/data
```

For defect-off, only after a valid and separately approved defect-on run completes, stop **both** SUT and agent. Set `$env:SUT_DEFECTS = ''` and restart Terminal A. Generate a **new** plan using the requester token with `plan --phase defect_off --users-file data/users-b3.json --out reports/b3/qwen3-1_7b/off-next`. Restart Terminal B with `QA_DATA_DIR` pointing to `reports/b3/qwen3-1_7b/off-next/data`. The human must separately review/approve that new run/hash and verify defect-off mode. Summarize/verify the `off-next` directory. Never reuse the on-plan approval for off execution.

Inspect actual selected test types: a per-product API action also opens Chromium for evidence, but that is not proof of a browser assertion for an unsupported UI requirement. Keep such requirements as coverage gaps. Counts are model-generated and must not be copied from replay. Compare failed oracle IDs with expected seeded defects, require zero defect-off false alarms and preserve errors/blocked cases.

## Full B2 benchmark after supervised success

The wrapper verifies both completed smoke runs from their original stores and audit bindings, not a self-declared success file. It requires distinct authenticated approval, faithful coverage, seeded-defect detection, no false alarms/errors/policy violations, successful API/browser audit events, report checksum and screenshot/trace/log hashes, and unchanged model digest/runtime/generation sources/settings. It then invokes the existing B2 harness; there is no second scoring framework. Separately authorized evaluation-grant enablement is excluded from the planning-configuration fingerprint, so the subsequent scoped-grant step can proceed without altering a reviewed plan.

```powershell
.\.venv\Scripts\python.exe scripts/local_model_b3.py benchmark --users-file data/users-b3.json --smoke-on reports/b3/qwen3-1_7b/on-next --smoke-off reports/b3/qwen3-1_7b/off-next --out reports/b3/qwen3-1_7b/full-next
```

This first full-harness invocation still pauses for its own authenticated first-plan review. Follow [B2 first-plan review and grant authorization](../b2/ENABLE_LIVE_EVAL.md). A real authenticated approver issues any scoped grant; the wrapper does not. Enable `unattended_eval_approval` in the local config only for a separately authorized grant scope. Preserve story/environment/skill/action/LIVE MODEL limits, expiry/use limits and external sealed-gold custody. Resume the same benchmark command with `--grant <actual-grant-id> --sealed-gold-dir <actual-custody-directory>`. Never use `--self-test-simulated-review` with a real model. A change of requester/digest or failed first plan requires a fresh reviewed handoff.

Without all required stories, repetitions, executions, verified held-out gold and thresholds, the verdict must remain NOT_EVALUABLE/SMOKE or FAIL. Even a complete harness PASS is **not independent quality certification**: the human-authored blind story, independent reviewer and QA-lead agreement/sign-off remain required. Review [contamination limitations](../b2/CONTAMINATION_AND_INDEPENDENCE.md).

Compare reports without further inference:

```powershell
.\.venv\Scripts\python.exe scripts/compare_models.py reports/b3/qwen3-1_7b/full-next/eval_report.json --out reports/b3/comparison
```

Add other model report paths only after separately approved installations and supervised runs for those exact models. B2 supplies AC precision/recall, coverage, skill/action validity, schema/repairs, gaps, injection/policy behavior, detection/false alarms, latency/usage and repeatability. B3 records native throughput and hardware samples where reported; unknown Intel GPU data stays unknown. No comparison score exists yet for a real local model.

## Failed attempts, tests and outstanding sign-offs

Initial local preflight: service unreachable, no generation performed, exit 3 / NOT_EVALUABLE. After user installation and approved pull, the real trial completed but failed semantic policy validation as recorded above. One CIM collection timed out under test memory pressure. An offline stub evaluation was interrupted to reduce RAM load and is incomplete; its recordings are not local-model inference. The real candidate passed JSON-schema validation but violated the provided oracle/action mapping. No provider/schema/security relaxation was justified or made.

| Validation | Result / evidence |
|---|---|
| Full existing suite | **363 passed**, one upstream Starlette/httpx deprecation warning; Windows/Python 3.13.14, 1,345.76 seconds. Invocation started at the merged baseline before B3 tests were added; the optional B3 CLI controls do not change its existing paths. Local receipt: ignored `reports/b3-baseline.xml` and log. |
| Targeted provider/B2/B3 regression run | **83 passed**, one upstream warning; local receipt `reports/b3-targeted.log`. |
| Final B3 contracts | **16 passed**, covering local-only/fixture-only boundaries, metadata-only probes, authenticated requester and distinct-reviewer enforcement, paused planning, contamination rejection, unavailable runtime, audit-bound phase metadata, pinned model versions, missing/tampered evidence and grant handoff. All are offline mocks, not reasoning evidence. |
| Documentation / syntax | Python compilation and CLI help checked; 57 local Markdown references resolved and all 15 PowerShell blocks in README/B3 guide parsed. Embedded Python configuration example parsed. No shared machine-specific defaults were changed. |
| Actual local model | Real native completion measured: 415.76 s, 4.899 generated tokens/s, 1,019 prompt / 1,896 generated tokens. **Planning FAIL** (invalid oracle/action pairing); full benchmark **NOT_EVALUABLE**. No approval/execution. |
| Offline evaluation / CI | [Successful complete workflow on implementation commit ef88e75](https://github.com/AnkitParekh007/grok-bots-agency/actions/runs/38768459633): **333 unit + 46 integration tests passed**, complete **30-run stub evaluation PASS**, 18/18 seeded-defect recall, zero defect-off false alarms, comparison and headless replay succeeded. The comparison remains ineligible for model-quality claims. Local browser-heavy attempts were interrupted under memory pressure; no partial local report is called PASS. Exact final-head checks are linked in the draft PR. |

CI remains offline and credential-free; tests use native-protocol mocks and temporary synthetic identities only. No model binaries, raw hardware/configuration dumps, credentials or machine-specific settings are committed.

Outstanding checkpoints: free sufficient RAM and obtain a valid fresh planning result; a distinct real approver must then create/control their own token, review the plan and separately approve defect-on/off runs. A larger model requires a separate download decision. Only successful supervised smoke permits a full benchmark/grant handoff. Sealed gold and independent human/QA-lead sign-offs remain outside this implementation's authority.
