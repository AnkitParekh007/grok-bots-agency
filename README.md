# Bluemeteor QA Agent — offline-first model providers

Local server references: [LM Studio structured output](https://lmstudio.ai/docs/developer/openai-compat/structured-output), [vLLM structured output](https://docs.vllm.ai/en/latest/features/structured_outputs/). Hosted quota values intentionally start unknown and must be configured from the approved account.

A local QA workspace: read a story, generate and validate a structured test plan, obtain human approval of its exact hash, execute Playwright/API checks against independent oracles, and export reports and evidence. Milestones A, B1, B2 preparation and C0 remain in place. Hosted inference is disabled by default.

## Local setup

Use Python 3.12+ and Chromium. On Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m pytest -q
```

On Linux/macOS use `.venv/bin/python` and, if needed, `python -m playwright install --with-deps chromium`.

Install and start Ollama yourself. The application **never downloads models**. In `config/agent.json`, set `providers.ollama.model` to the exact ID of an already installed instruction/chat model. The sample is `qwen3:8b`; availability is checked locally. Set `context_window` and `max_completion_tokens` to fit your model and hardware. Ollama receives native JSON schema, `num_ctx`, `num_predict` and `stream:false` requests. Leave Ollama cloud features disabled for local-only operation.

Start the synthetic SUT and workspace in separate terminals:

```powershell
.\.venv\Scripts\python.exe -m uvicorn sut.app:app --host 127.0.0.1 --port 8601
.\.venv\Scripts\python.exe -m qa_agent.server
```

Open `http://127.0.0.1:8600`. Model Settings shows authorization, availability, structured formats, limits, observed quota, performance, memory where reported, and the last evaluation. **Check connection** queries the selected approved endpoint. Discovery does not approve catalog entries. Choose an approved model for a **new** run; existing runs retain their identity. Review every plan before execution.

For a model-free demonstration, set `QA_MODEL_PROVIDER=replay`. `python scripts/demo_and_screenshots.py` explicitly uses replay and real Chromium. Replay/stub plans are labeled recordings and never count as real reasoning evidence.

## Provider compatibility

| Provider | Transport / structured format | Default | Verification |
|---|---|---|---|
| Ollama | Native `/api/chat`, JSON schema; tags/show/ps discovery, capabilities and memory | Enabled, loopback | Offline protocol tests; installed model required |
| LM Studio | Compatible chat, JSON schema or JSON object | Disabled, loopback | Mock contract; configure model and server context |
| llama.cpp | Compatible chat, `json_object` + schema for its grammar API | Disabled, loopback | Mock contract; chat template/schema support required |
| vLLM | Compatible chat, JSON schema or JSON object | Disabled, loopback | Mock contract; structured-output backend required |
| Groq | Official compatible endpoint, strict schema with catalog-derived params | Disabled | Mock HTTP; candidates require account verification |
| Gemini | Official `generateContent`, `responseJsonSchema`, native usage/finish reasons | Disabled, no default model | Mock HTTP; select a Flash model your free account supports |
| OpenRouter | Compatible chat, explicit `:free` ID, no fallback, required parameters | Disabled, no default model | Mock HTTP; verify structured-output support |
| NVIDIA NIM | Configuration placeholder only | Disabled / deferred | Access, permitted use and free availability unconfirmed |
| Replay / compatible stub | Recordings / transport self-test | Explicit demo/test selection | Never reasoning-quality evidence |

Groq candidates: `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`. Catalog entries are not approval or account entitlement. No Gemini or OpenRouter model has been assumed for your account. Generic priced Chat Completions remains disabled and preserves B1's monetary ledger for deliberately authorized future paid configurations. There is no automatic fallback.

Local engines need sufficient server-side context. The client checks a conservative UTF-8 token ceiling, output limits, and Ollama's reported capabilities/context. Other compatible servers rely on the administrator's capability manifest and enforce their own context allocation. Missing usage fails closed and retains the reservation. Version is persisted when reported (Ollama discovery digest, Gemini modelVersion, compatible version/fingerprint); otherwise it is explicitly unknown. Mutable tags are not immutable version guarantees.

## Optional free hosted inference

Hosted calls require all administrator controls: enabled and approved provider, approval reference, exact model, synthetic data class, credential environment variable, verified free-account reference, `account_tier:"free"`, `free_only:true`, explicit quotas, and the independent host policy. Keys belong only in the server process: `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`. The selector cannot change credentials, endpoints, policy, tier or approvals. Real Jira/Bitbucket/customer/production/confidential content is forbidden from hosted inference in this milestone, even if mistakenly added to the provider's data-class list.

Endpoints: `https://api.groq.com/openai/v1`, `https://generativelanguage.googleapis.com/v1beta`, `https://openrouter.ai/api/v1`. Redirects, proxy environment inheritance, automatic provider/model changes, paid upgrades and catalog-based selection are prohibited. Non-loopback local engines additionally require LAN approval, TLS and a separately approved host. Use an authenticated TLS reverse proxy for approved remote engines.

**Local counters cannot guarantee an account will not be billed.** Use an account/project with billing disabled or provider-side zero spend enforcement. Free entitlement, limits, permitted use and data handling vary and can change. Verify them before approval. Do not buy credits or attach billing to complete setup. Free APIs can throttle or remove models. Public claims are never authorization.

Quotas use persistent SQLite atomic reservations: RPM/RPD/TPM/TPD, configurable minute/day windows and reset offset, concurrency leases, observed remaining quota/reset headers, and Retry-After. Every retry reserves separately. Failed/unknown-usage attempts keep conservative token charges; only provably unsent calls are refunded. Account scopes prevent model changes resetting counters. Counters cover this installation, not other applications on the account. Monetary caps remain a separate ledger for billable adapters.

## B2 evaluation

```powershell
# Offline self-test; SIMULATED review, never model-quality evidence
python scripts/eval_b2.py --provider stub --self-test-simulated-review --out reports/eval/stub
# After local authorization; pauses for authenticated manual first-plan review
python scripts/eval_b2.py --provider ollama --confirm-live-approved --out reports/eval/ollama
# Other approved providers: groq, gemini, openrouter, lmstudio, llamacpp, vllm
python scripts/compare_models.py reports/eval/*/eval_report.json --out reports/eval/comparison
```

`QA_AGENT_CONFIG` may point to administrator-managed configuration. `--model` accepts only the primary ID or an approved model entry. Use a separate output directory per provider/model. Resume the first-plan handoff with `qa_agent.eval_authorization review` and an authenticated approver token, then a scoped server-side grant. See [approval procedure](docs/b2/ENABLE_LIVE_EVAL.md). Held-out keys remain sealed and hash verified. Defect-on/off execution, independent scoring, false alarms, injection checks, manual review and fail-closed verdicts remain required. Stub PASS is not model PASS. Comparison requires `real_reasoning_evidence:true` for quality claims.

## Preserved controls and integrations

Schema/catalog validation rejects invented skills, commands, endpoints, expected results and approval instructions. Plans bind to exact hashes; grants need authenticated approvers and bounded scopes. Ownership, cancellation, TTL, sequential SUT execution, append-only hash-chained audits, secret redaction, spending reservations, reports, traces and independent oracles remain. Audit chains detect tampering but are not signatures; administrators with DB/filesystem access remain trusted.

C0 Jira Cloud/Data Center and Bitbucket Cloud/Data Center adapters remain read-only and disabled. Project/story/repository/environment selection remains server validated. Bluemeteor Dev/Playground remain unverified and non-executable until existing safety checks are met. Production stays prohibited. This milestone grants no integration authorization.

[Historical documentation](docs/history/README-pre-B2-FREE.md) preserves previous milestone evidence; its provider instructions are superseded. See [delivery evidence](docs/b2-free/DELIVERY.md) for tests, limits and approvals. Mocks establish no model-quality claims.

References: [Ollama chat](https://docs.ollama.com/api/chat), [Groq structured output](https://console.groq.com/docs/structured-outputs), [Groq quotas](https://console.groq.com/docs/rate-limits), [Gemini generation](https://ai.google.dev/api/generate-content), [Gemini billing](https://ai.google.dev/gemini-api/docs/billing), [OpenRouter structured output](https://openrouter.ai/docs/guides/features/structured-outputs), [OpenRouter limits](https://openrouter.ai/docs/api/reference/limits), [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md). Verify the actual account; documentation is not account approval.
