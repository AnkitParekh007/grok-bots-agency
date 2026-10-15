# Local model setup

[README](../README.md) · [Hosted alternatives](SETUP_HOSTED.md) · [Security](ARCHITECTURE_SECURITY.md)

This guide runs a real model against synthetic fixtures. The adapter is implemented and tested with offline protocol mocks; no installed-model quality result is claimed. Complete the README's Python/dependency/Chromium setup first. Run commands from the repository root in PowerShell. Model installation is an operator action: the agent has no model-download tool.

## Ollama on Windows

### 1. Install and verify the service

Install the official [Ollama Windows application](https://ollama.com/download/windows). See [Windows requirements and setup](https://docs.ollama.com/windows) and [GPU support](https://docs.ollama.com/gpu) for your current OS, driver and device. Reopen PowerShell after installation so `ollama` is on PATH. The Windows app normally starts the local service in the background.

```powershell
ollama --version
Invoke-RestMethod 'http://127.0.0.1:11434/api/tags'
```

If the service is not running, open the Ollama app or run this in a separate terminal:

```powershell
ollama serve
```

Do not start a second server when the app already owns port 11434. Keep the endpoint on loopback and disable Ollama cloud features for a local-only workflow. Do not substitute a cloud-backed model tag.

### 2. Choose and manually pull a model

The repository's example is `qwen3:8b`. It is a candidate, not a benchmark winner. Check the current [Qwen3 model card/tags](https://ollama.com/library/qwen3), model license and completion capability before use. Download size is not total inference memory: weights, context/KV cache, runtime overhead and other applications all consume memory.

These are conservative **starting experiments**, inferred from model weight sizes and context overhead, not hardware guarantees or measured recommendations for this project:

| Machine class | Initial experiment | Tradeoff |
|---|---|---|
| CPU-only, roughly 16–32 GB system RAM | Quantized 1–4B chat model and a short context | May be slow or fail plan-quality requirements; RAM and CPU matter. |
| Around 8 GB VRAM | Quantized 4B class; consider 8B only if total memory fits | Long contexts or other GPU work can exhaust memory. |
| Around 12–16 GB VRAM | Quantized 7–8B class | Start with one request; measure memory and latency before expanding context. |
| Around 24 GB VRAM or more | Trial a quantized 14B class if its full working set fits | More parameters do not guarantee better valid plans. |

The configured 32,768-token context can require substantially more memory than a small-context model trial. This application's conservative input reservation counts UTF-8 bytes plus output allowance, so a very short context may reject a complete story/catalog even when a tokenizer would count fewer tokens. Choose a smaller model **with sufficient context**, rather than truncating safety instructions or the catalog.

For the default example, manually download it after deciding it fits:

```powershell
ollama pull qwen3:8b
ollama list
ollama show qwen3:8b
```

Use the exact installed name/tag in configuration. To try another size, manually pull its exact supported tag and change the configuration below to match. No model was downloaded as part of the documentation validation.

### 3. Create an untracked administrator configuration

This copies the checked-in defaults to ignored `data/`, leaving shared defaults intact. It is **not a dotenv file**. The JSON contains settings and environment-variable names, never credentials.

```powershell
@'
import json
from pathlib import Path
cfg = json.loads(Path('config/agent.json').read_text(encoding='utf-8'))
cfg['model']['provider'] = 'ollama'
p = cfg['providers']['ollama']
p.update(model='qwen3:8b', base_url='http://127.0.0.1:11434',
         context_window=32768, max_completion_tokens=4096)
Path('data').mkdir(exist_ok=True)
Path('data/agent-local.json').write_text(json.dumps(cfg, indent=2), encoding='utf-8')
'@ | .\.venv\Scripts\python.exe -
```

Edit `model`, `context_window`, `max_completion_tokens` and, if measured necessary, `timeout_s` in this copy to fit the installed model/server. The inherited loopback provider has `enabled:true`, `approved:true`, `approval_ref:"loopback-local-default"`, `classification:"local"`, `cost_type:"local"`, `api_key_env:null` and synthetic-only allowed data. Keep those restrictions. Quotas also apply locally and are separate from context capacity.

### 4. Start the SUT and agent

In Terminal A:

```powershell
$env:SUT_DEFECTS = 'all'
.\.venv\Scripts\python.exe -m uvicorn sut.app:app --host 127.0.0.1 --port 8601
```

In Terminal B:

```powershell
$env:PYTHONUTF8 = '1'
$env:QA_AGENT_CONFIG = (Resolve-Path 'data/agent-local.json').Path
$env:QA_MODEL_PROVIDER = 'ollama'
$env:QA_AUTH_MODE = 'demo'
.\.venv\Scripts\python.exe -m qa_agent.server
```

`QA_AGENT_CONFIG` is read by the server and evaluation CLI. In the server an explicit JSON configuration determines the default provider; set its `model.provider` correctly rather than relying on an old process override. Clear `QA_AGENT_CONFIG` before returning to the README replay walkthrough. Demo auth is only for a trusted local demonstration; use [local auth](ARCHITECTURE_SECURITY.md#local-authentication) for authenticated separation of duties.

### 5. Generate, review and execute a real plan

1. Open [the workspace](http://127.0.0.1:8600). In **Model Settings**, select `ollama / qwen3:8b` (or your exact configured ID).
2. Click **Check connection**. The adapter discovers `/api/tags`, checks `/api/show` for completion/context support and reads `/api/ps` for loaded-memory observations. Discovery does not install, load or approve a model; memory may be unknown until the server loads it.
3. Select project `PCC`, story `PCC-DEMO-1` and `synthetic-local`. Send “Read PCC-DEMO-1, generate test cases, and show coverage gaps.” The selected model generates the plan; ensure the run is not labeled REPLAYED or STUB ENDPOINT.
4. Inspect source quotes, assumptions, uncovered requirements, positive/negative/edge coverage, skill actions and fixture/oracle references. Reject an incorrect plan and start a fresh run after correcting configuration or requirements. A valid schema alone does not establish a good test plan.
5. Review the plan hash and click **Approve & execute** only for the plan you accept. The runtime runs registered synthetic browser/API actions and compares against independent oracle values. A real model can choose different tests, so the replay demo's exact result counts are not promised here.
6. Open **Report (HTML)**, **Report (JSON)** and the evidence links from the completed run. With the default data directory files are in `data/runs/<run-id>/`; `QA_DATA_DIR` changes that root. The JSON checksum is the adjacent on-disk `report.json.sha256` file. View reports through the workspace so evidence links resolve correctly.

Model identity is retained per run. Ollama's observed digest is recorded when available; mutable tags are not a promise of an immutable model version. Changing the selector affects only new runs.

## Other local servers

Install and run these servers separately using their official instructions. The agent does not provision them. All three adapters are disabled by default, and have mock contract coverage rather than completed real-model evaluation.

| Provider ID | Default loopback endpoint | Official server/protocol reference | Requirement |
|---|---|---|---|
| `lmstudio` | `http://127.0.0.1:1234/v1` | [LM Studio structured output](https://lmstudio.ai/docs/developer/openai-compat/structured-output) | Load a completion/chat model, enable its local server, verify schema or JSON-object mode. |
| `llamacpp` | `http://127.0.0.1:8080/v1` | [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) | Correct model/chat template and grammar/schema support; adapter uses `json_object` with schema for this dialect. |
| `vllm` | `http://127.0.0.1:8000/v1` | [vLLM structured output](https://docs.vllm.ai/en/latest/features/structured_outputs/) | Supported structured-output backend and adequate server-side context; server platform/hardware setup is separate from the Windows client. |

In an administrator JSON copy, set `model.provider` to the provider ID. In its `providers` entry set `enabled:true`, `approved:true`, an actual `approval_ref`, the **exact served model ID**, `context_window`, `max_completion_tokens` and verified `structured_output` (`json_schema` or `json_object`). Keep loopback, local classification/cost and synthetic-only data. Set `QA_AGENT_CONFIG` to the copy and restart the agent. Probe and review/execute as above. `/v1/models` discovery must report your configured ID; discovered entries are not automatically approved. A compatible server's capability manifest and context allocation are the administrator's responsibility.

Remote local servers are a different trust boundary: require explicit `remote_local_approved:true`, an approval record, HTTPS, the exact model host in `config/policy.json`, and a nonempty gateway credential environment reference in `api_key_env`. Use an authenticated TLS gateway; merely changing a loopback URL to a LAN IP is insufficient. [Security details](ARCHITECTURE_SECURITY.md).

## Troubleshooting local inference

| Symptom | Action |
|---|---|
| Connection refused/timeout | Check service/PATH/port, `/api/tags`, endpoint and firewall. Increase a timeout only after measuring expected latency; do not silently switch provider. |
| Configured model unavailable | Match the exact catalog ID. Pull/load it manually in the server; restart/probe after correcting configuration. |
| GPU memory exhausted | Stop other GPU jobs, unload unused models, use a smaller quantized model, or reduce context/output while keeping room for the complete prompt. Inspect `ollama ps` and the server logs. Partial CPU offload may be slow. |
| Context too small | Check model-reported capacity, server allocation and configured context/output together. The client uses a conservative byte ceiling. Do not drop policy/catalog content to force a request. |
| Malformed/truncated JSON | Verify chat template/schema support and output allowance. The default runtime permits at most two format repairs; policy violations are not repaired. Review events and reject invalid plans. |
| Missing usage / unknown version | Missing required usage fails closed and preserves a conservative reservation. Unreported version stays unknown; do not invent a version, throughput or memory reading. |
| Quota blocks a local model | Inspect Model Settings and reservation/reset values. Correct limits only with capacity evidence; unknown/failed usage can retain token reservations. |

For real benchmarking, use [B2 first-plan approval](b2/ENABLE_LIVE_EVAL.md), separate output directories per provider/model, and authenticated review/grants. The ordinary interactive tutorial is not a completed blinded quality evaluation.
