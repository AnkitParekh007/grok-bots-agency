# Manual regression suites and evidence gates

This optional capability creates immutable suite versions, registers every child attempt before planning, seals a batch against further attempts, and aggregates local evidence. It is disabled by default: the operator must set `regression.enabled` to `true` in an authenticated workspace configuration. Demo identities and service identities cannot use it. This is a manual workflow; it adds no scheduler, CI execution grant, automatic approval, deployment hook or organization fixture isolation.

A suite does not authorize a run. Every child goes through existing source/model/domain policy, planning, exact distinct-human approval, package verification and execution admission. No corporate integration is enabled by this feature.

## Publish a version

Use completed, authenticated, independently reviewed template runs in the same tenant/project. The publisher needs the live project approver role and must differ from every template requester. Template approval must match its exact execution-envelope hash. A template can contain a failure: the suite records its reviewed contract, not a passing baseline claim.

1. POST `/api/projects/{project}/regression-suites/{suite}/versions/{version}/preview` with `slots` (one to twenty objects containing `id`, `run_id`, `plan_hash`), `max_age_s` (60–604800), and an aware ISO `expires_at` within seven days. Review the returned definition and `definition_hash`.
2. POST the same body and that exact `definition_hash` to `/api/projects/{project}/regression-suites/{suite}/versions/{version}`. Changed inputs or an existing version fail. GET that path to inspect the published definition.

Identifiers contain only ASCII letters, numbers, underscores and hyphens, up to 80 characters. Duplicate slots/templates are rejected. The definition identifies its publishing human. Each slot binds the complete reviewed envelope: story/AC/context/plan hashes, model identity, target, policies/configuration, connection scopes, domain approval, oracle/fixtures, action/catalog/schema/code and dependency fingerprints. Only the requester is removed from the comparison so another authorized requester can rerun the contract. Source/model output or any other contract change requires a new reviewed suite version. Nondeterministic planning may therefore yield UNKNOWN rather than a comparable result.

Publication retains the publisher's server-created authorization receipt, never the credential itself. The publisher must remain a live project approver when opening/using the version or aggregating a batch. Session expiration, logout, credential expiration/rotation, role changes, suite expiration and feature disablement fail closed. The suite's seven-day maximum lifetime and live receipts provide no persistent workload delegation.

## Run and seal a batch

POST `/api/projects/{project}/regression-suites/{suite}/versions/{version}/batches` with `suite_hash`. The authenticated human requester owns the resulting `batch_id`.

POST `/api/regression-batches/{batch_id}/attempts` with `{"slot":"selected-slot-id"}`. Inputs come from the pinned slot; the caller cannot substitute a model, target, repository or story. The run, append-only attempt record and queued planning job (when configured) share one SQLite admission transaction. Local planning also starts only after that transaction commits. Failed registration rolls back the run before any model/worker starts. Each slot permits at most three attempts. Every new attempt still needs its own human plan approval.

The owner can POST `/api/regression-batches/{batch_id}/seal` once. Sealing is irreversible and prevents further attempts, including from another process. It can seal an incomplete batch, which cannot pass. A new batch is required to add attempts after sealing. SQL triggers prevent updates/deletes of suite versions, batches, attempts and seals; these controls assume the deployed database/filesystem remain protected from an administrator replacing them. A non-success response after a committed admission does not erase that attempt. Audit projection failure before local planning retains the admitted record for inspection, starts no model, and cannot yield a passing child; reconcile rather than assuming the request was rolled back.

## Interpret a gate

The owner or a live project approver can POST `/api/regression-batches/{batch_id}/gate`. It performs local reads only, with no source, model, SUT or deployment request. All attempts are discovered from the admission ledger; the request accepts no selected result list or latest-attempt filter.

| Verdict | Meaning |
|---|---|
| PASS | Sealed batch, every slot represented, every recorded attempt completed and fresh, exact suite contract and independent approval, all cases passed, no acceptance-coverage gaps, and required evidence verified. |
| FAIL | At least one comparable completed attempt has a failed case with verified required evidence. Later passing attempts do not erase it; missing siblings cannot turn it into PASS. |
| UNKNOWN | Missing slot, unsealed batch, pending/cancelled/error/blocked/interrupted child, stale or changed inputs/approval, missing/inconsistent evidence, coverage gap, or evidence work budget exhaustion. |

Version/authorization/restore failures return a non-success HTTP response. Consumers must treat every non-success, timeout, unavailable dependency, missing result and unrecognized verdict as UNKNOWN; none permits release. An expired version cannot produce a fresh positive gate. A definite failed attempt may still produce FAIL while another attempt is unknown.

The gate checks report age independently of run updates, its checksum and agreement with stored envelope, approval, criteria/context, summary, gaps and case results; oracle/configuration/fixture/source snapshots; complete case IDs; and screenshot/trace/log byte lengths and digests from the report manifest. Each result must retain its plan's AC/oracle references, match nonempty independent oracle expectations with a finite nonnegative tolerance, and agree with a fresh deterministic comparison of its recorded actual values, status and differences. This comparison checks the recorded observation; it does not contact the SUT or independently prove those actual values were measured. The gate rechecks run state and authorization after evidence reads. Limits are twenty slots, sixty total attempts, fifty cases per attempt, 500 manifest entries, 100 fixture entries, 2 MB JSON reports/envelope/config/source reads, 20 MB individual evidence/fixture/oracle reads, 100 MB combined reads and an observed ten-second evidence-read/comparison deadline. These are application work bounds; database lock waits, filesystem system calls and OS quotas need deployment qualification.

`gate_hash` addresses the returned observation, including all attempt/report/plan hashes and its timestamp. `release_authorized` is always `false`. PASS means only this declared suite's verified recorded cases passed at that observation. It does not qualify the model, prove an authentic vendor commit, guarantee actual organization behavior, authorize deployment, or constitute a signed external attestation. Protect the local state/evidence and retain the gate with its exact release candidate and suite hash in any separately reviewed release process.

## Recovery and remaining work

Schema v3 adds append-only suite tables; older supported databases migrate transactionally. Encrypted backup/restore preserves this history. A global authorized restore boundary invalidates old versions/batches for new use; reconcile and create fresh template reviews/versions/batches. No restored suite causes execution or replay.

ER-21 remains partial. Organization scheduling and bounded/revocable execution grants, scoped CI/workload identities, independently reviewed fixture isolation/cleanup, actual parallel SUT contracts, release-candidate attestation and external fail/unknown consumer qualification remain open. Synthetic constructed-evidence tests validate aggregation semantics; they are not real model reasoning or Supplier Portal execution evidence.
