# Audit delivery, checkpoints and recovery

SQLite is the authoritative local audit ledger. Each hash-chained row and projection-outbox record commit together. Sequence allocation takes SQLite's write lock across separate connections/processes. The gateway commits `tool.intent` before an allowed call and links its outcome by invocation ID. An intent without an outcome means **unknown**, not success or proof that no external effect occurred.

Global and per-run JSONL files are projections. Ordered delivery uses fsync and per-record acknowledgement. Retrying a complete line written before acknowledgement checks its exact sequence/content and avoids duplication. File failures leave durable pending records. The gateway stops dispatch at 1,000 pending projections by default (`policy.audit_delivery.max_pending`). Corrupt/partial lines are not silently rewritten.

Preserve the database, projections and partial test evidence during incidents. With planning/execution quiescent, explicitly repair projections:

```powershell
.\.venv\Scripts\python.exe -m qa_agent.audit_checkpoint repair-projections --data-dir PATH_TO_AGENT_DATA
```

This opens the existing database without a runtime, refuses active runs, verifies the chain, preserves previous bytes under `audit-recovery/<receipt-id>/`, and rebuilds projections. Paths remain inside the designated data directory. The SQLite write lock excludes concurrent ledger changes during replacement. A partial repair can be rerun. More than 10,000 run projection files need an archival recovery procedure. The command does not execute/resume runs or repair a corrupt authoritative ledger.

Use an independently controlled Ed25519 signing key for checkpoints. `QA_AUDIT_SIGNING_KEY` contains base64 of the 32 raw private bytes; a separately pinned public-key file contains base64 of the 32 public bytes. Keep private keys out of Git, reports and chat. Supply an explicit stable deployment ledger identity:

```powershell
.\.venv\Scripts\python.exe -m qa_agent.audit_checkpoint checkpoint --data-dir PATH_TO_AGENT_DATA --ledger-id DEPLOYMENT_LEDGER_ID > CHECKPOINT.json
.\.venv\Scripts\python.exe -m qa_agent.audit_checkpoint verify --data-dir PATH_TO_AGENT_DATA --ledger-id DEPLOYMENT_LEDGER_ID --public-key PINNED_PUBLIC_KEY.txt --receipt CHECKPOINT.json
```

The signature binds identity, count, head/sequence and a hash of the complete canonical prefix, including legacy rows. Verification can compare the signed prefix with a later database. Changed signatures/identities, shortened history and altered rows fail. Read-only checkpoint commands do not interrupt runs.

Store checkpoints and their trust record in independently controlled, retention-protected storage. No such sink or key custody has been configured/qualified here. A signature beside a mutable database does not establish external immutability; control of the signing key allows signing replacements. External delivery, key rotation/custody, workflow-state/audit atomicity and measured recovery exercises remain acceptance work.
