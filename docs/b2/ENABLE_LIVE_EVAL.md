# Enable the live Grok evaluation: checklist

Do these in order. Stop at any unchecked item. Nothing in this repo enables Grok by default.

**Prerequisites (approval)**
- [ ] `LIVE_EVAL_APPROVAL_REQUEST.md` is signed: sponsor, security, legal, QA lead, xAI admin.
- [ ] Items D1–D6 (data handling) and P1–P3 (pricing/billing) are verified, with dates recorded in the approval doc.
- [ ] The QA lead has approved the oracles (`fixtures/oracles/PCC-DEMO-2..6`) and gold annotations (`fixtures/eval/gold/*`). Update the `review` blocks.
- [ ] The QA lead has agreed or adjusted the thresholds in `qa_agent/eval_metrics.py` (`PROPOSED_THRESHOLDS`).
- [ ] PR #1 (B1 + review fixes) is merged, or the eval is run from this branch with an explicit note in the report.

**xAI account**
- [ ] An organisation-owned xAI team exists, with a dedicated evaluation API key.
- [ ] A team spend limit or prepaid credit of ≤ $15 is set, if the console supports it.
- [ ] The ZDR decision is recorded (not required for synthetic data).

**Local configuration** (on the eval workstation only; do not commit secrets)
- [ ] `config/agent.json` → `providers.grok`:
  - `enabled: true`, `approved: true`
  - `approval_ref: "<sign-off reference>"`
  - `model: "grok-4.7"` (or the approved model)
  - `pricing`: input 2.00, cached 0.50, output 6.00 USD per 1M tokens, or the current verified price
- [ ] `budget`: `per_run_usd: 1.0`, `daily_usd: 15.0`.
- [ ] `config/policy.json` → `tools.model.request.approved_hosts == ["api.x.ai"]` and `require_https: true`.
- [ ] `export XAI_API_KEY=...` in the shell that runs the harness only. Never put it in files, CI or chat.
- [ ] `python -m pytest -q` is green and `python scripts/eval_b2.py --provider stub --runs 1` passes.

**Run**
- [ ] Smoke: `python scripts/eval_b2.py --provider grok --confirm-live-approved --stories PCC-DEMO-5 --runs 1 --no-execute`.
  - The report shows `LIVE MODEL` in the model labels, `unpriced_calls: 0`, and cost > 0 from actual usage.
  - The audit has `model.request` rows with correlation IDs to `api.x.ai`.
- [ ] Full: `python scripts/eval_b2.py --provider grok --confirm-live-approved --runs 3`.
- [ ] Check the stop conditions (approval doc §8) and archive `reports/eval/` and the audit export.

**After**
- [ ] Roll back the config (approval doc §9) and revoke the key.
- [ ] Post the results and the threshold verdict for review. Do not change thresholds after seeing results without recording why.
