# B2-FREE delivery evidence

Branch: `feature/b2-free-local-llm-providers`, target `main`. No merge, deployment, production configuration change, credit purchase, paid registration, model download or real hosted inference call is authorized or performed.

## Baseline

Latest main at inspection: `f50f51c`. Milestones A, B1, B2 preparation and C0 are present; PRs #1–#3 were merged. No prior B2-M multi-provider branch or PR existed. The separate baseline checkout was kept unchanged while tests ran.

The full Windows/Python 3.13.14 baseline ran 307 tests: **294 passed, 13 failed** in 1329.51 seconds. See [machine-readable baseline](baseline.json). Six failures concerned Windows fixture containment/execution, three concerned UTF-8 evaluation output, and four concerned C0 mock path/newline assumptions. The migration corrects these portability issues without weakening approval, ownership, oracle, data or egress controls.

## Implementation

- Replaced the provider-specific runtime/configuration and credentials with generic Chat Completions plus native Ollama and Gemini protocols. Historical milestone documentation is preserved byte-for-byte under `docs/history/`; replay fixtures and sealed gold are preserved.
- Added local discovery, availability and capability checks, configurable context/output limits, bounded transport behavior and measured token/performance reporting. Native Ollama supports loaded-memory reporting and discovery digests. No download endpoints are implemented.
- Groq, Gemini and explicit OpenRouter `:free` models are optional and disabled. NVIDIA NIM is deferred; access, permitted use and free availability have not been confirmed.
- Added account-scoped persistent quota reservations, observed rate-limit/reset headers, Retry-After and concurrency leases, separate from the monetary reservation ledger.
- Added a read-only Model Settings registry and approved per-run selection. Exact model identity/version, or explicit unknown version, is persisted in runs, reports and model audit entries. Selection cannot change administrator policies or existing runs.
- Generalized B2 live-provider scoring, contamination checks and identity-bound first-plan resume. Added offline report comparison; recordings and incomplete reports never support quality claims.

The [README](../../README.md) contains the compatibility matrix, local setup, free-tier limits and current official protocol references. The [approval template](../b2/LIVE_EVAL_APPROVAL_REQUEST.md) grants no authorization.

## Validation

- Initial new provider/quota contracts: 41 passed.
- Final targeted providers, quotas, priced-provider regressions and C0 contracts: **156 passed** (offline, including mock local HTTP servers).
- Clean full unit suite, Chromium integration suite, UI evidence and exact-head CI: results are being recorded in this document before delivery.

All test connections are restricted to loopback or in-process mock transports. CI sets replay as the default, has no inference credentials, and blocks real hosted providers even if configuration is changed. Tests cover missing/invalid usage, invalid structured responses, credentials, quotas, rate limits, unavailability/retries, billing modes, classification, egress/redirects, selection/injection, secrets, approvals, ownership, scoring and real execution evidence.

## Outstanding approvals and limitations

- No real model-quality evaluation was performed. An operator must install a suitable model independently, configure its exact local ID, and perform authenticated first-plan review before real evaluation. No local model was downloaded.
- Hosted enablement requires organizational synthetic-egress authorization, actual free-account/model entitlement, configured quotas and independent HTTPS host approval. No Gemini or OpenRouter account model has been assumed.
- Free account status cannot be proven by local counters or response metadata. Provider-side billing disablement/zero-spend controls are a separate prerequisite; account limits, data policies and availability can change.
- Real Bluemeteor/Jira/Bitbucket/customer/production/confidential data is denied to hosted inference throughout this milestone.
- Compatible local servers need administrator-verified structured capabilities and server-side context allocation. Unreported model versions, memory, remaining quotas or evaluation scores remain unknown.
- SQLite scopes cover one installation, not other applications using the same provider account. Administrative filesystem/DB access remains trusted; audit chains are not signatures.
