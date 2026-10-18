# Offline backup, restore and schema compatibility

This is a bounded single-host recovery foundation. It does not certify production RPO/RTO, managed key custody, a storage service, filesystem power-loss guarantees or disaster recovery ownership. Stop every instance and external writer sharing the data directory before backup. Keep the source and destination on private storage with approved volume encryption and filesystem ACLs. Restored evidence and the temporary SQLite snapshot are plaintext within that protected storage; session encryption and encrypted backup files do not encrypt the live evidence/database volume.

## Backup set and key

The backup includes a SQLite backup-API snapshot, run artifacts, global audit projection, preserved audit-recovery files and the prior restore receipt if present. The manifest binds file names, lengths, SHA-256 hashes, schema version, verified audit-ledger fingerprint, explicit ledger identity and recovery-tool hash. Configuration/release artifacts, local credential files, model weights, session/signing/client secrets and the backup key must be recovered separately through their designated owners. Never commit them or paste them into chat.

`QA_BACKUP_KEY` must reference a base64-encoded 32-byte random key supplied by the operator's secret manager. Keep the key separately from backups; record its vault version in an approved external inventory. Rotation requires retaining access to the key versions needed for retained backups. Losing that key makes the encrypted archive unusable. The tool does not install or configure a vault.

```powershell
.\.venv\Scripts\python.exe -m qa_agent.recovery backup --source <stopped-data-directory> --destination <new-private-backup-file.qab> --ledger-id <stable-deployment-ledger-id> --confirm-quiescent
.\.venv\Scripts\python.exe -m qa_agent.recovery restore --source <backup-file.qab> --destination <new-private-restored-directory> --ledger-id <same-ledger-id>
```

The tool is operator-only and is unavailable through HTTP/model tools. The backup requires an explicit stopped-writer attestation and rejects active planning/execution or running jobs. A database write barrier, SQLite snapshot and repeated artifact inventory/hash checks detect ordinary concurrent changes; this is not an online backup protocol. Publication refuses existing destinations. Source data is preserved on failures. Private generated staging is bounded and cleaned on handled failures; abrupt process termination can leave staging for operator inspection and secure disposal under the data policy.

The versioned container uses AES-256-GCM authenticated records of at most 1 MiB with sequence/header binding, a fresh 256-bit random HKDF salt deriving a separate archive key, and an authenticated final record. It detects changed keys/content, reordered records, truncation and appended bytes. A custom container still requires independent security review before production approval. Restoration authenticates each record before consuming plaintext and publishes only after the complete manifest, file hashes, ending, SQLite integrity and audit fingerprint verify. Budgets are 2 GiB of state, 10,000 inventory entries and a 4 MiB manifest; larger installations need a separately reviewed archival design. No truncation or partial success bypass exists.

API references: [AES-GCM authentication](https://cryptography.io/en/latest/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM), [HKDF](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#hkdf), [SQLite backup API](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup).

## Restore without reviving old authority

Restore only into a new directory. It never overwrites the current deployment. Verification first preserves the original ledger prefix and artifact bytes. A new audit record then reports maintenance invalidation: prior web sessions, OIDC login flows, source discovery/selection receipts, eval grants and accepted approvals are invalidated. Rejected approval records are appended without rewriting review history. Pending jobs and reviewable/approved runs become blocked and require fresh requests/reviews. Existing resource quarantines remain. Uncertain mutations are never replayed.

Before starting the restored application, compare its original audit prefix against an independently retained signed checkpoint, verify the required deployment configuration and secret versions, rebuild any damaged projections using the explicit preservation procedure, reconnect reviewed source scopes, and reconcile target quarantines. Keep the old data directory and evidence under the retention/incident policy. Do not redirect production traffic until the designated operator has completed the agreed checks. `ready_for_deployment: false` in the restore receipt is deliberate.

## Schema and rollback

`PRAGMA user_version` records application schema version 3. Version 2 added Jira write receipts; version 3 adds append-only manual suite versions, batches, attempts and seals. Existing supported databases upgrade in one schema transaction; an exception rolls back table/column changes. A database from a higher schema version is refused before mutation. The version metadata uses SQLite's [application-owned user_version](https://www.sqlite.org/pragma.html#pragma_user_version).

This release supports additive legacy-to-v3 migration, not arbitrary downgrade. Restored manual suite history remains readable to operators, but old versions/batches cannot grant new admission or a positive gate; reconcile and create fresh reviews. To roll back an incompatible deployment, stop its writers and restore a verified pre-upgrade set into a new directory with its matching release/configuration and external secret inventory. Versions predating this guard cannot enforce it; a deployment supervisor must prohibit launching those against a newer database. Production rollback and corruption drills remain acceptance work.

## Synthetic drill and acceptance

```powershell
.\.venv\Scripts\python.exe scripts/recovery_drill.py --out reports/enterprise-readiness/recovery-drill
```

The script builds eight explicitly synthetic completed runs and evidence files in a fresh report subdirectory, backs them up, restores them and compares every artifact hash plus an Ed25519-signed original audit prefix. Its key is ephemeral and intentionally discarded after the drill. The report records observed backup/restore durations and explicitly denies production RPO/RTO qualification. It performs no model inference, organization access or SUT requests.

Agree backup cadence/RPO, recovery objective/RTO, retention/legal hold, external immutable storage, key recovery, owner/on-call roles and operating-system cleanup before real-data rollout. Then perform a supervised restore from independently stored media on the actual deployment, including lost/different keys, disk full, corrupt data, incompatible release and target-side-effect reconciliation. Unit/synthetic checks alone do not close ER-11 or ER-17.
