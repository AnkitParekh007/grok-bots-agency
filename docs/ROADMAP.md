# Roadmap and delivery status

[README](../README.md) · [Implementation evidence](b2-free/DELIVERY.md)

Status describes implementation, not production readiness. “Delivered” does not imply approved real-service access or validated real-model quality.

| Milestone | Status | Delivered scope / remaining boundary |
|---|---|---|
| A — Synthetic execution foundation | Delivered | Fixture stories/replay, registered import/attribute skills, synthetic SUT, Playwright/API checks, independent oracles and evidence reports. |
| B1 — Secure planning/runtime foundation | Delivered for local prototype | Structured planning/validation/gaps, exact plan approvals, local token roles/ownership, budgets, audit, cancellation and bounded runtime. Enterprise deployment controls remain outside scope. |
| B2 — Model evaluation preparation | Delivered framework; real evaluation pending | Synthetic stories/gold, defect-on/off runs, scoring, contamination checks, first-plan review and scoped grants. Thresholds and independent human review remain pending. |
| C0 — Read-only integration foundation | Delivered adapters; real access pending | Jira/Bitbucket Cloud/Data Center adapters, mock contracts, workspace selectors and environment gates. Real integrations disabled; Dev/Playground unverified. |
| B2-FREE — Multi-model/local inference | Delivered implementation; evaluation pending | Native Ollama/Gemini, compatible local/Groq/OpenRouter paths, quotas, registry/per-run selection and comparison. Optional hosts disabled; NVIDIA deferred; no real quality certification. |
| C1 — Approved real Jira/Bitbucket access | Approval-gated, not completed | Obtain service-account/scope approval, verify real contracts and data handling. Real-story classification/model authorization must be resolved explicitly. |
| C2 — Repository-grounded QA | Planned | Use approved repository evidence to ground planning. Current read-only context snapshots are recorded but not model input. |
| C3 — Environment testing | Planned / safety-gated | Verify nonproduction URLs, synthetic accounts, cleanup and actual registered environment-specific checks before execution. |
| C4 — Human-approved Jira write-back | Planned | Design explicit reviewed defect/comment/transition operations with identity, scope and audit. No write adapters currently exist. |
| C5 — Regression and release sanity automation | Planned | Approved suite orchestration, scheduling, report comparisons and release gates; current individual reruns are manual. |

Immediate follow-up work is actual operator-installed local-model evaluation, QA-lead agreement on thresholds, independent blinded human stories/review, and separately authorized account/access checks. No dates, hosted access entitlement, model download, spend or production deployment are implied by this roadmap.
