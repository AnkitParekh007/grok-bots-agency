# Live Grok evaluation — approval request (Milestone B2)

Status: **DRAFT, awaiting sign-off.** The proposed numbers below are **not** an approved authorization. Grok remains disabled in `config/agent.json` (`enabled: false`, `approved: false`, pricing `null`). No live xAI call has been made by this project.

## 1. Scope
- **Purpose:** measure how well a live Grok model plans tests, using the offline harness `scripts/eval_b2.py` and the metrics and PROPOSED thresholds in `qa_agent/eval_metrics.py`.
- **Data:** the five SYNTHETIC stories in `fixtures/eval/eval_set.json` (PCC-DEMO-2…6), the synthetic dictionary (`attribute_dictionary.json`, synthetic-0.2), the registered skill/action/oracle catalog, and synthetic CSV file names.
  - No customer data, no real PCC stories, no credentials, no personal data.
  - Two stories (PCC-DEMO-3 and PCC-DEMO-6) contain deliberate prompt-injection text addressed to the agent.
- **What leaves the box:** one prompt per HTTP attempt. Each prompt holds the system rules, the catalog and one story, about 6–7 KB, or roughly 2.1k–2.4k tokens plus the JSON schema in `response_format`.
- **Contamination guard:** hand-authored stub answers exist in `fixtures/replay/` for harness self-tests only.
  - They are never placed in prompts; `tests/test_b2_eval.py::test_prompt_never_contains_recorded_answers` checks this.
  - In `--provider grok` mode the harness refuses loopback/stub endpoints and counts any plan identical to a recorded answer as a hard-gate failure.
- **Out of scope:** production or customer environments, Jira/Git integrations, and any write to external systems.
- **Approval modes** (enforced in the runtime, see `qa_agent/eval_authorization.py`):
  1. **Manual (default and unchanged).** The **first live-generated plan** is planned by the harness, persisted (run id + exact plan hash in `reports/eval/eval_state.json` and the DB), and the harness exits `PAUSED/AWAITING_FIRST_PLAN_APPROVAL`. An authenticated approver (local auth, not the requester) approves or rejects that exact plan hash with `python -m qa_agent.eval_authorization review` (token from `QA_TOKEN`). Re-running the harness resumes only after it verifies a manual, authenticated approval of the unchanged plan; rejection or an altered plan ends as `NOT_EVALUABLE`. The harness never approves the first plan itself, and the state survives restarts. This is always required for the `LIVE MODEL` class.
  2. **Unattended (synthetic eval only).** This needs `unattended_eval_approval.enabled=true` plus a **server-side eval grant** created by an authenticated approver (`POST /api/eval-grants` in local auth mode, or `python -m qa_agent.eval_authorization create`). The creator identity comes from the auth session; identity fields in the request are ignored. A grant is bound to stories, environment, skills, actions and model classes, and has an expiry and a use limit. Uses are counted atomically, approvers can revoke a grant at any time, and creation, use, denial and revocation are all audited. Edited rows (hash mismatch) and rows without an audited creation are rejected. The earlier unsigned JSON authorization file has been **removed**; there is no file-based path.

## 2. Data classification
| Item | Classification | Notes |
|---|---|---|
| Eval stories, oracles, gold, dictionary, CSV names | `synthetic` | authored for this project; `allowed_data_classes: ["synthetic"]` is enforced by the data-class gate |
| Model outputs (plans) | `synthetic` derived | stored locally in `reports/eval/data` (SQLite + artefacts) |
| API key | secret | server environment only (see §5) |

## 3. xAI data handling: items to confirm
Pages retrieved 2026-10-11. **Re-verify each item at approval time.** The retrieved pages carry the branding "SpaceXAI"/"SpaceXSI". Confirm the contracting entity name in the agreement.

| # | Statement in the docs | Source | Action |
|---|---|---|---|
| D1 | By default API requests and responses are stored encrypted for 30 days for abuse/misuse auditing, then deleted automatically | https://docs.x.ai/developers/faq/security , https://docs.x.ai/console/faq/security | Confirm this is acceptable for synthetic data |
| D2 | Inputs and outputs are not used for training without explicit permission. Some free-credit offers may require permission to train | https://x.ai/legal/faq-enterprise | Confirm no credit or training opt-in applies to our team |
| D3 | Zero Data Retention (ZDR) is team-wide, can be self-served by a team admin, and disables Files, Collections, Batch and stateful Responses | https://docs.x.ai/developers/faq/security | Decide whether ZDR is required. It is not needed for synthetic data; the harness uses only stateless chat completions |
| D4 | Personal data may only be sent through the ZDR-enabled API (DPA / Enterprise Terms) | https://x.ai/legal/data-processing-addendum | We send no personal data. Confirm with legal |
| D5 | Inputs and outputs may be retained longer if legally required or flagged for ToS/AUP review | https://x.ai/legal/faq-enterprise | Acknowledge |
| D6 | Data region / processing location | not found in the pages above | **VERIFY** with xAI or account docs |

## 4. Account and keys
- **Account:** an organisation-owned xAI team (not a personal account), with Ankit or a delegate as team admin.
- **Key:** a dedicated API key for this evaluation only, scoped to that team.
- **Spend limit:** set a team spending limit or prepaid credit in the xAI Console if available (**VERIFY** that this feature exists). It is the external backstop for the local caps.

## 5. Key handling
- `XAI_API_KEY` is set only in the environment of the process running `scripts/eval_b2.py` or the server. It is never put in files, config, git, CI, chat or logs.
- The provider registers the key for redaction. Tests show the key never appears in DB rows, events, reports, API responses or audit entries.
- GitHub Actions has no xAI secret, and CI runs the stub only.
- The key is revoked in the xAI Console after the evaluation (see §9).

## 6. Pricing and proposed budget caps
**Price source:** https://docs.x.ai/docs/models (retrieved 2026-10-11), USD per **1M tokens**.

| Model | Input | Cached input | Output |
|---|---|---|---|
| `grok-4.7`, prompt < 200k tokens | $2.00 | $0.50 | $6.00 |
| `grok-4.7`, prompt ≥ 200k tokens | $4.00 | $1.00 | $12.00 |

Our prompts are about 2.5k tokens, far below the 200k long-context threshold.

**Items to verify on the pricing and API pages:**
- **(P1)** Are reasoning tokens billed at the output rate? The code currently bills `completion_tokens + reasoning_tokens` when `total_tokens` implies it.
- **(P2)** Does `max_completion_tokens` bound reasoning tokens too? If not, the worst case per request is **not bounded** by our config, and the console spend limit (§4) becomes mandatory.
- **(P3)** Are failed requests (429/5xx) billed? We conservatively reserve them as billed.

**Configured ceilings** (`config/agent.json`):
- `max_completion_tokens` = 8,000
- `max_retries` = 3 globally (unchanged), so up to 4 HTTP attempts per model call. The eval harness applies `config/eval_profile.json` (`max_retries: 1`, eval only). The reviewer recommends this only **after** pricing and reasoning limits and the external spend backstop are verified; it does not replace those approvals.
- `repair.max_format_repairs` = 2, so up to 3 model calls per run

**Worst case per HTTP attempt.** The prompt is taken as 4,000 tokens (measured about 2.4k, plus margin for the schema):
- input 4,000 × $2/1M = $0.008
- output 8,000 × $6/1M = $0.048
- **total $0.056**

**Worst case per run:** 3 calls × 4 attempts × $0.056 = **$0.672** with the default `max_retries=3`. With `max_retries=1`, which is **recommended for the eval**, it is 3 × 2 × $0.056 = **$0.336**. The runtime reserves this per attempt and refuses any attempt that would cross a cap.

**Eval run plan:** (5 required + 1 held-out story) × 3 runs × 2 phases (defect-ON and defect-OFF) = **36 runs**.
- Worst case: 36 × $0.336 = **$12.10** with `max_retries=1`, or $24.19 with `max_retries=3`, which **exceeds the $15 total cap**. The cap would stop the eval, and the result would be NOT_EVALUABLE.
- Realistic: about 36 × $0.023 ≈ **$0.83**. This is an estimate only; the report uses actual usage.

**Proposed caps, all PROPOSED (not approved):**

| Cap | Proposed | Where enforced |
|---|---|---|
| per-run | **$1.00** | `budget.per_run_usd`, atomic per-attempt reservation |
| daily | **$15.00** | `budget.daily_usd` |
| total eval | **$15.00** | `budget.total_usd` for `budget.scope` (e.g. `b2-live-eval-1`). It is **persisted in SQLite across runs, days and restarts**, and reserved atomically (`BEGIN IMMEDIATE`) |
| provider backstop | ≤ $15 | xAI Console team spend limit or prepaid credit. This is a **separate prerequisite** (**VERIFY** availability); the local caps do not replace it |

The repo config now defaults to $1 / $15 / $15 with scope `b2-live-eval-1`. A billable provider refuses to start if any cap or the scope is missing, zero or non-numeric.

## 7. Egress
- Host: `api.x.ai` over **https** only, path `/v1/chat/completions`.
- The allowlist is in `config/policy.json` → `tools.model.request.approved_hosts`, separate from the provider config.
- Every attempt is audited with a correlation ID (`X-Request-ID`).

## 8. Run plan
1. Dry run on the stub: `python scripts/eval_b2.py --provider stub --runs 3` (CI already runs this).
2. **Smoke (plan only, live):** `--provider grok --confirm-live-approved --stories PCC-DEMO-5 --runs 1 --no-execute`. The verdict is always `SMOKE/NOT_EVALUABLE`. Check the audit, usage and cost rows.
3. **First-plan handoff (live):** `--provider grok --confirm-live-approved --out reports/eval` plans the first plan and exits 4 (paused). The approver reviews it and runs `review ... --decision approved|rejected`, then creates the grant (`max_uses` ≥ 35).
4. **Full evaluation (live, resume):**
   - Command: `--provider grok --confirm-live-approved --out reports/eval --grant <grant_id> --sealed-gold-dir <custody dir>`.
   - This is 36 runs: a defect-ON phase (`SUT_DEFECTS=all`) and a defect-OFF phase (no defects).
   - **PASS requires** every required story, at least 3 runs, execution results for every run in both phases, the verified sealed gold, and every threshold measured and met.
   - Anything incomplete is `NOT_EVALUABLE`; any hard-gate or threshold miss is `FAIL`.
   - Failed or missing runs are kept in every denominator.
5. Review `reports/eval/eval_report.md` and `.json` against the PROPOSED thresholds. Hard gates are:
   - `policy_violations` = 0
   - `false_alarms` = 0
   - `gap_recall` = 1.0
   - `contaminated_plans` = 0
   - `defect_off_runs_incomplete` = 0
   - Also review `near_copy_*` (heuristic, see `CONTAMINATION_AND_INDEPENDENCE.md`) and do independent reviewer spot checks (`INDEPENDENT_REVIEW_TEMPLATE.md`).
6. Archive the report together with an audit export (`python -m qa_agent.audit_verify export`).

**Stop conditions:**
- any hard-gate failure in the smoke run;
- any `unpriced_calls` > 0 (missing usage);
- cumulative cost > $5 before the full run completes;
- any unexpected egress denial.

## 9. Rollback
1. In `config/agent.json`, set `providers.grok.enabled=false` and `approved=false`, and restore `pricing` to `null`. The provider then fails closed again.
2. Unset `XAI_API_KEY` and revoke the key in the xAI Console.
3. Delete `reports/eval/data`, or keep it per the retention decision.

There are no other systems to roll back: no deployment, no CI secret, no external writes.

## 10. Sign-off
| Role | Name | Decision (approve / reject / conditions) | Date |
|---|---|---|---|
| Sponsor / budget owner | Ankit Parekh | | |
| Security | | | |
| Legal / data protection (D1–D6) | | | |
| QA lead (oracles + gold annotations, thresholds) | | | |
| xAI account admin (org account, key, spend limit, ZDR decision) | | | |

## Sealed holdout custody (PCC-DEMO-7)
- The sealed gold stays at `/workspace/bluemeteor-sealed/` only (directory mode 0700, file 0600). It is not tracked in git (a test checks this) and is not referenced by CI, so CI cannot read it. It never goes into GitHub, CI logs or model prompts.
- Handover: only to a **named QA lead / evaluator** (name: ______), through an approved private channel (name: ______), and only at scoring time. The sha256 in `fixtures/eval/eval_set.json` is verified when scoring.
- **Prerequisites for a final (non-provisional) verdict:** a human-authored blinded story with its own sealed gold (`CONTAMINATION_AND_INDEPENDENCE.md`), and QA-lead approval of all oracles and gold annotations. Until both exist, any live PASS is provisional.
