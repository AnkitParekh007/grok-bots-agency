# Bounded evidence publication

Application evidence now has explicit byte/file limits and disk-reserve checks. This addresses part of ER-25 and ER-11; it does not implement encrypted live storage, retention, legal hold, deletion, a managed vault or a filesystem quota. Existing evidence is not automatically removed to admit new work.

## Limits and admission

The root `storage` object in operator-managed `agent.json` accepts these positive integer settings. Limits cannot be disabled with zero or a boolean; unknown settings and inconsistent bounds fail startup.

| Setting | Default | Meaning |
|---|---:|---|
| `max_file_bytes` | 20,000,000 | Maximum published file; operator ceiling 64 MB |
| `max_run_bytes` | 100,000,000 | Combined run evidence bytes |
| `max_total_bytes` | 1,000,000,000 | Combined evidence and private capture staging bytes |
| `max_run_files` | 500 | Run file count, including empty files |
| `max_total_files` | 10,000 | Aggregate file count; operator ceiling 100,000 |
| `minimum_free_bytes` | 100,000,000 | Free space preserved before writing a full new replacement |
| `inventory_timeout_s` | 5 | Cooperative inventory/manifest traversal deadline; maximum 30 seconds |

File ≤ run ≤ total byte limits and run ≤ total file limits are mandatory. The reserve must be at least 1 MB. Choose actual limits from an approved deployment capacity plan, including audit/database and native capture headroom. Arbitrary operators editing the filesystem are outside the application's quota coordination boundary.

Inventory streams directory entries with entry/file/time bounds, rejects symlinks, junctions, hard links and special files, and counts abandoned staging/atomic-write remnants. Existing oversized evidence or an unreadable/unsafe tree stops admission; it is not silently skipped. Manifest hashing streams bounded chunks, checks observed length and enforces its own deadline. These are cooperative deadlines checked between filesystem operations, not interruption of a blocked kernel/filesystem call.

Runtime publication holds the shared SQLite `BEGIN IMMEDIATE` write barrier while it checks the actual file inventory and atomically replaces the destination. Separate runtime instances sharing that database cannot both spend the same observed capacity. New content is written to an exclusively created private temporary file, flushed/fsynced, fenced again and published with `os.replace`. An I/O/fencing failure before publication preserves the previous destination. Filesystem publication and SQL transaction commit are not a distributed atomic commit, and directory-entry crash durability still depends on the filesystem; later envelope/content checks remain required.

Replacement charges its final byte/file delta, but requires free space for the entire new temporary file while the old version still exists. The standalone `FileArtifactStore` helper without a `Store` has only a process-local lock; concurrent services must use the runtime's database-backed form. Out-of-band writers and mixed application revisions are unsupported.

## Screenshots, traces and missing evidence

Screenshots are obtained as bytes and published through the bounded writer. Native traces are first generated under `data/artifact-staging`, outside the run artifact HTTP namespace. After generation, the app checks regular-file identity/size, reads at most the publication limit plus one byte, and publishes through the same quota/fence path. Only that call's newly created temporary file is cleaned up. Existing abandoned captures remain for operator review.

**The native producer can consume memory/disk before it returns.** The application publication ceiling does not hard-limit Chromium/Playwright buffers, native trace staging, SQLite, stdout logs or the global audit projection. A managed encrypted volume with verified ACLs, filesystem/container quotas, a bounded native worker and independent supervision remain deployment requirements. A published-evidence limit is not proof of total process/disk isolation.

Screenshot, trace and log are required for an executed test. If any cannot be saved, the result has `evidence_complete=false`, status `error`, the prior comparison outcome in `comparison_status`, and a reason naming missing evidence. Subsequent cases are `not_run`; there is no automatic replay or continuation of mutations. A completed workflow with error/not-run results is not an acceptance pass. Operators must reconcile target effects before a fresh request/review; these controls do not implement Supplier Portal compensation or cleanup.

Cancelled cases that never execute remain `not_run` and do not claim execution evidence. Partial published artifacts and durable database/audit records remain available subject to existing authorization. An oversized/failed private capture is not advertised as valid trace evidence.

Approval-package verification also checks whether the plan's known screenshot/trace/log and report file count can fit. It accounts for existing destinations without creating/reserving files. A known impossible file budget is refused before execution approval. Capture byte sizes and future concurrent use remain unknown, so this preflight does not replace publication-time limits or reserve a successful run's capacity.

## Readiness and operator recovery

HTTP readiness now includes `artifact_capacity`; protected health/metrics include aggregate `artifact_bytes`, `artifact_files` and `artifact_staging_files` without run, story, project or tenant labels. New direct/HTTP requests, phase entry, approval-package verification and tool dispatch recheck capacity. Dispatch checks again after durable intent delivery, since delivery can consume capacity. Audit delivery and cleanup remain independent so uncertainty can be recorded and resources closed. HTTP admission failures return a generic 503; health does not run a model/source/SUT probe.

Per-run audit projections count in the observed inventory, but audit delivery is deliberately not routed through the bounded replacement writer. The authoritative audit/database and global projection need their own protected capacity/retention arrangements. Stop admissions and diagnose aggregate counts, disk space, audit backlog and filesystem integrity. Do not delete published evidence, audit history or unknown staging files merely to clear readiness. Retention, holds, authorized disposal, tenant fairness, storage backend scale and independent recovery qualification remain open in the [implementation ledger](IMPLEMENTATION_STATUS.md).

Offline backup excludes private native staging from the state allowlist. Stop all workers, preserve any uncertain captures separately under the incident/evidence policy, reconcile target effects and follow [recovery operations](RECOVERY_OPERATIONS.md). A restored application re-inventories published files; no persisted quota counter can authorize capacity that the restored filesystem lacks.

Validation uses small synthetic limits, injected disk/fsync/rename/fence failures, separate database connections competing for capacity, HTTP/direct admission denial and real Chromium against the owned synthetic SUT. Full production-scale inventory, multi-process/native disk exhaustion, eight-hour soak, filesystem quotas/encryption and organization cleanup remain unqualified.
