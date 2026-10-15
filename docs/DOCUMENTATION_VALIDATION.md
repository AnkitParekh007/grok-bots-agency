# README redesign: validation and before/after

Documentation baseline: merged B2-FREE commit `4ff99dd72ede5865756f39c47c32a1a876a18549` (PR #4). Review date: 2026-10-15. This follow-up changes current documentation and adds one real UI capture; it does not change runtime/provider behavior, fixtures, oracle/gold data, policy or dependency files.

## Before and after

| Before | After |
|---|---|
| Provider migration notes were the main entry point. | Required project title, plain-language purpose, prototype scope and 16-topic navigation. |
| Implementation and validation limits were scattered. | Capability/status matrix separates synthetic availability, experimental paths and planned work. |
| Windows setup omitted cloning and mixed default Ollama with a model-free demo. | Copy-paste clone/venv/dependencies/Chromium and two-terminal replay walkthrough with expected defect-on/off behavior. |
| Local setup assumed installation/model/context knowledge. | Dedicated Windows Ollama install/pull/configure/probe/review/execute/report tutorial, hardware trial guidance and troubleshooting. |
| Hosted setup summarized controls without a per-service walkthrough. | Dedicated Groq/Gemini/OpenRouter process-environment, account verification, configuration and policy instructions; NVIDIA explicitly deferred. |
| Screenshots existed but were not a guided entry point. | Reused actual replay/evidence captures plus a current project/story selection capture, with limitations in captions. |
| Architecture, security, roadmap and contributor information were brief or historical. | Mermaid trust boundaries and focused linked guides for auth, environments, mocks, extension points, CI and milestones. |
| No explicit licensing statement in the entry point. | Absence of `LICENSE` documented without inventing a license or claiming licensed open source. |

## Validation record

| Check | Result |
|---|---|
| Fresh Windows environment | `python -m venv .venv`, requirements installation and Playwright Chromium installation succeeded; clone URL matches the public repository/remote and `git ls-remote origin HEAD` matched the merged baseline. |
| Windows quick-start UI | Real headless Chromium drove project/story/repository selection, replay planning, the approval gate, execution and report links. Defects on: 3 passed / 5 failed / 0 error / 0 blocked. Defects off: 8 passed / 0 failed / 0 error / 0 blocked. |
| Reports and audit | Both report JSON checksums verified; HTML/JSON/audit links returned HTTP 200. SQLite and JSONL audit chains verified with the documented audit CLI (91 records each). |
| Unit/provider/planning/security suite | `pytest -q -m "not integration" --junitxml=reports/readme-unit.xml`: **317 passed**, 46 integration cases deselected, one upstream Starlette/httpx deprecation warning. |
| Full offline B2 evaluation and comparison | README commands completed successfully: **PASS** across 30 repeated defect-on/off stub runs, complete self-test, no planning failures, 18/18 seeded-defect recall and zero defect-off false alarms. `real_reasoning_evidence:false`; comparison `quality_claim_eligible:false`. Usage/price/latency are simulator observations with fake pricing, not real-model performance or spending. |
| Markdown and examples | 87 relative links/anchor/image references across nine current Markdown files resolved; all 25 PowerShell fences parsed with PowerShell's language parser. Embedded Python example parsed, executed and resulting Ollama JSON verified without connecting to a model. |
| Mermaid and GitHub Markdown | Mermaid 11 parsed and rendered the exact README diagram to SVG in Chromium; visually inspected. GitHub Markdown API rendered headings, tables, details and the Mermaid fence. The API omits heading IDs; TOC/target anchors were checked separately. |
| Official external references | 14 unique documentation URLs checked and reachable via direct HTTP or the web/browser service. Direct checks encountered transient Groq connection resets and a vLLM HTTP 429; those pages were successfully opened through the web service, and a later direct vLLM request succeeded. No permanent availability guarantee. |
| CLI and mock services | Evaluation/auth/review options checked against CLI help/source. Documented standalone model and integration mock commands started on ports 8701/8702; fake-key Chat Completions returned the expected local-stub fingerprint. |
| Scope and provenance | No runtime xAI/Grok references found in `qa_agent/`, `config/`, requirements or CI; no tracked license file found. Historical Markdown content unchanged from the merged baseline. Only current docs and one new synthetic screenshot are changed. |

Local command outputs are retained in ignored `reports/` (unit JUnit/log, `eval/readme-stub/`, comparison JSON/Markdown, `readme-ui/validation.json`, link/parser receipts and Mermaid SVG/PNG). Replay/stub output remains harness evidence only. The full 46-case integration suite is left to the unchanged CI workflow; the local documentation walkthrough independently exercised real Chromium and API execution in both defect modes.

The local environment uses Windows, PowerShell and Python 3.13.14. GitHub's existing workflow independently uses Python 3.12/Ubuntu, both unit/integration suites, offline evaluation/comparison and a headless replay demo. The PR checks provide exact-head CI evidence; [workflow definition](../.github/workflows/qa-agent-ci.yml).

## Deliberate validation boundaries

- Local-model installation/pull and real inference are **operator instructions checked against source and official protocol/setup documentation**, not actions executed or model-quality claims in this follow-up.
- Hosted settings were checked against the implemented gates and current public official documentation. No provider key, real account entitlement, billing state, hosted inference, integration access or production environment was used/verified.
- Interactive credential entry/user-token commands were inspected and syntax/CLI checked; token output was not published. Provider commands that require installed external software (`ollama`) were not claimed as locally run.
- Existing historical evidence/screenshots and sealed gold were not rewritten. Old milestone labels in historical images are identified as historical; the current UI capture uses replay.
- Local filesystem/anchor/image checks and Mermaid rendering establish documentation structure, not all GitHub renderer behavior or permanent external-link availability. Links were checked on the review date; external sites can change or reject automated clients.
