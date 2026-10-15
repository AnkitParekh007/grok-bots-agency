# Enable an approved B2 provider evaluation

Previous provider instructions are preserved in `docs/history/b2/` and are superseded.

Install dependencies using the [Windows quick start](../../README.md#quick-start--windows),
then configure an administrator-approved [local model](../SETUP_LOCAL.md) or
[hosted account](../SETUP_HOSTED.md). Commands below assume that environment's
interpreter is active (or replace `python` with `.\.venv\Scripts\python.exe`).
Use [local authenticated roles](../ARCHITECTURE_SECURITY.md#local-authentication)
for review/grants, not demo-mode labels. A normal interactive plan approval is
separate from the evaluation harness's first-plan review and scoped grant.

1. Record provider/model approval, synthetic-only purpose and scope. For hosted providers, verify the actual free account, permitted use, retention/training policy and zero-billing protection. Organizational authorization is required even for synthetic egress. No real Bluemeteor content is permitted in hosted inference in this milestone.
2. An administrator sets `enabled`, `approved`, `approval_ref`, exact `model`, version where available, structured capability, context/output limits and quotas. Hosted configurations also require `free_only:true`, `account_tier:"free"` and `account_verified_ref`. Add the exact HTTPS host to the independent model policy. The selector cannot change policy.
3. Set credentials in the server process only. Loopback Ollama needs no key. Remote local engines need LAN approval, TLS and a policy-approved host. Keep hosted providers disabled in CI.
4. Plan-only smoke: `python scripts/eval_b2.py --provider ollama --confirm-live-approved --stories PCC-DEMO-5 --runs 1 --no-execute --out reports/eval/ollama`. Expect SMOKE/NOT_EVALUABLE; inspect usage, identity and audit.
5. Full first-plan handoff: `python scripts/eval_b2.py --provider ollama --confirm-live-approved --out reports/eval/ollama-full`. Exit 4 means PAUSED/AWAITING_FIRST_PLAN_APPROVAL. An authenticated approver reviews the exact plan and runs the printed review command with their token. Requesters cannot self-approve.
6. An authenticated approver creates a scoped grant with `qa_agent.eval_authorization create`, limited to stories, environment, skills/actions, LIVE MODEL class, expiry and maximum uses. Resume with `--grant <id> --sealed-gold-dir <custody-directory>`. Never simulate review for real models. Keep sealed gold out of prompts and Git.
7. Only complete PASS reports with real reasoning evidence count as model evaluation. Compare with `scripts/compare_models.py`. Replay/stub results describe harness behavior only. Do not reuse output directories for another provider/model.
8. Revoke grants and unset credentials when the scope ends. Disable hosted providers and remove host policy when approval ends. No deployment or production changes are part of this procedure.
