# Owner-selected contextual evidence

Organization planning can include explicitly selected repository files, Jira comments and text attachments alongside the approved issue fields. This is an opt-in source and validation contract, not autonomous repository discovery or evidence of real model quality. No organization selection or data/model grant is enabled by this change.

## Selection and authority

Start with [`config/context-sources.example.json`](../../config/context-sources.example.json). It is disabled review material and is not loaded automatically. Add completed selection records to the existing workspace project's owner-approved `source_policy.contexts` list only after reviewing the exact source text and intended model/data use. The source-policy tenant/classification/reviewer/reference/expiry and exact model, endpoint, region, retention/training-use grant remain mandatory. Missing model/data approval denies before context collection. Synthetic labels cannot import or silently ignore selected organization contexts.

| Kind | Exact reviewed selection | Scope |
|---|---|---|
| `repository_file` | `repository`, full lowercase 40-character `commit`, relative ASCII `file`, `sha256` | Must equal the run's selected repository and belong to its workspace project |
| `jira_comment` | Numeric `id`, `sha256` | Only the run's source issue |
| `jira_attachment` | Numeric `id`, `sha256` | Only the run's source issue; supported complete UTF-8 text with matching declared/observed bytes/hash |

All rows include `kind`; extra keys and duplicate resource selections are rejected. Branch names, abbreviated commits, traversal/absolute/URL/encoded paths, model-selected locations, web crawling, archive/image/PDF interpretation and arbitrary downloads are unsupported. The source adapters remain GET-only and retain their connection/project/repository and live identity/job controls. Bitbucket's [source endpoint](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-source/) accepts the reviewed commit in the existing read-file route; the application records that exact request and observed content rather than claiming an independently attested Git object.

The required digest covers the adapter-observed UTF-8 text before the context transform. Jira ADF has already been flattened by the adapter; this is not a hash of the original rich JSON. Repository checkout line-ending conversion can produce a different digest from the pinned upstream response. Obtain/review exact text through an approved read mechanism before approving its digest. There is no automatic selection approval or fallback to a newer revision. IDs, hashes or file names found inside untrusted content cannot authorize another read.

The collector snapshots the policy/selection and rechecks source approval before and after each read. Source and model-grant changes during collection cannot issue a valid model permit. Authors, unselected comment text, attachment filenames and unrelated metadata are excluded from model input. The normalized comment API may retrieve a bounded comment collection to locate the selected IDs; unselected entries are discarded and are not planner sources. Actual human-versus-service ACL equivalence still needs organizational qualification.

## Bounds and provenance

The collector accepts at most 10 sources, 100 KB of observed/transformed text per source, 200 KB combined context and a 300 KB total issue/context bundle. Comment and attachment inventories are bounded to 200 entries / 200 KB. Attachment type/declared size is checked before body retrieval; a missing, partial, binary, malformed, oversized or changed source fails instead of silently truncating or omitting evidence. A 60-second observed collection budget is checked around each adapter operation. Individual operations retain their adapter transport/collection limits; this is not an OS-level hard deadline or network sandbox.

Model text normalizes CRLF/CR to LF and redacts registered secret values/patterns. Each entry retains its observed-text digest, transformed-text digest, classification, locator, available upstream timestamp/full requested commit and line bounds. This redaction is not a general DLP or PII classifier; owners remain responsible for selecting suitable content and authorizing its disclosure. Only valid timestamp metadata is retained. Content-derived IDs and hashes identify the snapshot; they do not prove upstream completeness, service honesty or actual ACLs.

Selected context enters the same explicitly untrusted model-input bundle as the approved issue fields. The exact context and policy are bound to the process-local model permit. Adding/changing content or source IDs after authorization invalidates that permit. Context never becomes an acceptance criterion, expected result, executable action, permission or approval. Only issue-field requirements and independent oracle/action catalogs have those existing roles.

## Citations, review and handoff

When `context_ids` is present, the model must return `context_review` for every selected source exactly once. A `used` row needs the exact ID/hash, verbatim citations with LF-text line ranges and references to existing test cases. A `not_used` row needs an explicit reason, no citations and a matching out-of-scope coverage gap. Missing review is repairable within existing limits; fabricated sources, versions, lines, quotations or test references are policy violations. Citation validity establishes provenance, not that a test correctly reasons about or covers the source.

The workspace plan panel and human review dialog display source locators, hashes, decisions and escaped quotations. JSON and HTML execution reports retain contextual review. The complete bundle remains in protected run evidence and is bound into the execution envelope. At review and dispatch, the runtime re-reads only the exact selected resources, rechecks their owner approval/digests and compares them with the captured corpus. Changed or unavailable context invalidates handoff before SUT execution; no automatic source update or reapproval occurs.

Remaining qualification includes actual Cloud/MCP mappings and ACLs, upstream immutable-version guarantees where available, data-owner decisions, managed evidence storage, adversarial real-model evaluation, context-window/latency limits and independent domain correctness. Existing top-level repository context is still separately observed and can invalidate a package when it changes. This feature does not mean the whole repository, all issue comments/attachments or the Supplier Portal documentation site has been ingested.
