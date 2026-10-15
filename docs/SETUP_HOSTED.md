# Optional free hosted inference

[README](../README.md) · [Local setup](SETUP_LOCAL.md) · [Security](ARCHITECTURE_SECURITY.md)

Groq, Google Gemini and explicit OpenRouter free routes are implemented, offline-contract-tested adapters. They are **disabled and unapproved by default**. No real hosted call or account entitlement was verified during this milestone. Groq is a separate service from xAI Grok; no xAI runtime/key is needed.

## Account and authorization prerequisites

Before configuring a service, an administrator must verify the actual account's free entitlement, exact model access, structured-output support, quotas, permitted/commercial use and retention/training policy. Record the decision and evidence references. Authorize synthetic egress under your organization's process. Free APIs may change terms, remove models or throttle; a public free-tier advertisement is not account approval.

Use provider-side billing disablement or an enforceable zero-spend control where available. If the account cannot satisfy the free-only requirement, leave it disabled. Do not attach billing or buy credits to make this walkthrough work. Local counters cannot guarantee that an account will never be billed, and do not include usage by other applications. This project does not certify any provider's privacy/commercial terms.

Only synthetic inputs are permitted in hosted inference in B2-FREE, even if someone adds another class to a configuration list. Real Jira/Bitbucket/customer/production/confidential material is prohibited. Real tracker content is classified `unclassified`, not silently relabeled synthetic.

## Process environment credentials

The application **does not load `.env` files**. Set keys only in the same server process's environment, or have an approved secret manager inject them. Do not place values in JSON, browser code, command arguments, screenshots, Git or logs. These examples prompt without putting the secret in PowerShell history; the process environment still holds plaintext accessible to a trusted local administrator.

Choose just the provider you are authorized to configure:

```powershell
# Groq
$qaSecret = Read-Host 'Groq API key' -AsSecureString
$env:GROQ_API_KEY = [System.Net.NetworkCredential]::new('', $qaSecret).Password
Remove-Variable qaSecret
```

```powershell
# Google Gemini
$qaSecret = Read-Host 'Gemini API key' -AsSecureString
$env:GEMINI_API_KEY = [System.Net.NetworkCredential]::new('', $qaSecret).Password
Remove-Variable qaSecret
```

```powershell
# OpenRouter
$qaSecret = Read-Host 'OpenRouter API key' -AsSecureString
$env:OPENROUTER_API_KEY = [System.Net.NetworkCredential]::new('', $qaSecret).Password
Remove-Variable qaSecret
```

Stop the server and remove the selected key when finished, for example `Remove-Item Env:GROQ_API_KEY`. Restarted processes do not inherit later changes to the parent shell's environment.

## Administrator configuration

Create an ignored JSON copy after the account checks:

```powershell
New-Item -ItemType Directory -Force data | Out-Null
Copy-Item config/agent.json data/agent-hosted.json
```

Edit that copy as UTF-8 JSON. `QA_AGENT_CONFIG` overrides agent settings only: the independent model host policy remains the repository's `config/policy.json`. There is no `QA_POLICY_FILE` override. Back up/review any deliberate policy change locally and do not commit a real account configuration.

| Field in `providers.<selected-provider>` | Required value |
|---|---|
| `enabled`, `approved` | `true` only after authorization |
| `approval_ref` | Your actual synthetic-egress/model approval reference |
| `model` | Exact account-supported and approved model ID; see service sections |
| `classification`, `cost_type`, `free_only` | `"hosted"`, `"free"`, `true` |
| `account_tier`, `account_verified_ref` | `"free"` and your actual verification evidence reference |
| `allowed_data_classes` | `["synthetic"]` |
| `api_key_env` | The service's environment variable **name**, never its value |
| `structured_output` | Verified `"json_schema"` (default); compatible JSON-object mode requires deliberate capability verification |
| `context_window`, `max_completion_tokens` | Verified model/account-supported limits; size for the full prompt |
| `quotas.rpm`, `rpd`, `tpm`, `tpd` | Explicit positive limits derived from the account; defaults are `null` and fail closed |
| `quotas.concurrency` | Start at `1`; never exceed verified capacity |

Keep the checked-in official `base_url`. In `config/policy.json`, add only the selected service's hostname to `tools["model.request"].approved_hosts`, without a scheme/path or wildcard. This is independent of provider enablement. Keep HTTPS required. Then set `model.provider` in your administrator JSON to the selected provider ID.

If a service does not publish one dimension (for example tokens per day), choose an explicit conservative organizational cap for that dimension and record the rationale; do not invent a provider entitlement or use an unlimited sentinel. Local caps must stay within every known account limit.

For example, **after** filling every required field and approving the host, start Groq from the shell containing its key:

```powershell
$env:PYTHONUTF8 = '1'
$env:QA_AGENT_CONFIG = (Resolve-Path 'data/agent-hosted.json').Path
$env:QA_MODEL_PROVIDER = 'groq'
.\.venv\Scripts\python.exe -m qa_agent.server
```

For Gemini/OpenRouter change both JSON `model.provider` and the environment selection to `gemini`/`openrouter`. An explicit JSON configuration controls the server default. Start the synthetic SUT as in the [README](../README.md#quick-start--windows). Model Settings lists the selected model's permission/health and reason when denied. A connection probe contacts the provider and can consume quota; execute it only within approval. Generate a synthetic plan, inspect it and approve its exact hash before execution. No hosted setup grants permission for a real blinded benchmark; use [B2 authorization](b2/ENABLE_LIVE_EVAL.md) separately.

## Groq

- Provider ID `groq`; key reference `GROQ_API_KEY`; base `https://api.groq.com/openai/v1`; policy host `api.groq.com`.
- The checked-in primary candidate is `openai/gpt-oss-20b`; alternative catalog entries are **not approved**. A candidate name is not verified availability or free entitlement. Confirm an exact supported model in your account before approving it.
- Adapter uses strict JSON schema with action parameters derived from the registered skill catalog; verify current model support in [Groq structured-output documentation](https://console.groq.com/docs/structured-outputs).
- Read [Groq account rate limits](https://console.groq.com/docs/rate-limits) and enter your actual limits. Do not copy another account's quotas.

## Google Gemini

- Provider ID `gemini`; key reference `GEMINI_API_KEY`; base `https://generativelanguage.googleapis.com/v1beta`; policy host `generativelanguage.googleapis.com`.
- No model ID is assumed. Choose an exact **Flash** model confirmed for your free account with structured output. The adapter rejects a model ID without `flash` in its name.
- Uses native `generateContent`, `responseJsonSchema`, usage and finish reasons; see the [generation API](https://ai.google.dev/api/generate-content). This is not Gemini's compatible Chat Completions endpoint.
- Verify [free/paid tier and billing distinctions](https://ai.google.dev/gemini-api/docs/billing) and your [account rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), including region/model availability and data-use terms. A free tier is not a guarantee of confidential-data handling.

## OpenRouter

- Provider ID `openrouter`; key reference `OPENROUTER_API_KEY`; base `https://openrouter.ai/api/v1`; policy host `openrouter.ai`.
- Select an exact account-supported model ending in `:free`. `openrouter/free`, automatic routing and paid alternatives are not accepted substitutes. No model ID is assumed by default.
- Verify [structured-output support](https://openrouter.ai/docs/guides/features/structured-outputs) for the model/provider route and current [free-route/account limits](https://openrouter.ai/docs/api_reference/limits). The adapter sets `allow_fallbacks:false` and `require_parameters:true`.
- Verify both router and downstream provider data policies and permitted use. A free suffix does not establish privacy, reliability or unlimited availability.

## NVIDIA NIM deferred

`providers.nvidia` is a disabled configuration placeholder, not a usable implemented provider. `NVIDIA_API_KEY` is a reserved environment-reference name only. Developer access, permitted use and free availability have not been confirmed; the builder explicitly refuses enablement. Do not set a key or enable it based on this guide. A future implementation needs verified access, protocol coverage and separate authorization.

## Limits and failure handling

Persistent SQLite quota reservations track RPM/RPD/TPM/TPD and concurrency within this installation/account scope. The configured minute/day windows and day reset offset are local enforcement settings: align them with verified service behavior. Observed remaining/reset values are shown only when returned by the service. Unknown external quota stays unknown. The client reserves a conservative UTF-8 input ceiling plus maximum output, so it may reject a request that would fit the provider's tokenizer limit.

Each retry reserves separately; HTTP 429 respects Retry-After with bounded retries/backoff. Unknown/failed usage retains conservative charges, while provably unsent calls can be refunded. Missing required usage fails closed. Quota counters do not replace the independent dollar ledger used by the deliberately priced generic adapter. There is no silent model/provider switch, paid upgrade, redirect following or proxy-environment inheritance.

On missing credentials, unapproved hosts, unknown quotas or unexpected billing mode, fix the authorization/configuration evidence or keep the service disabled. Do not bypass checks. Hosted providers are blocked in CI. At approval expiry stop the agent, unset keys, disable the provider and remove its model-host approval.
