# Supplier Portal domain onboarding

Owner inputs recorded on 2026-10-16: Bitbucket repository [`bluemeteorapps/supplierportalui`](https://bitbucket.org/bluemeteorapps/supplierportalui/src), [test](https://supplier.pcctest.bluemeteor.com/), and [playground](https://supplier.pccplayground.bluemeteor.com/#/login). The application is the Supplier Portal / SKU Onboarding Portal (SOP). Both environments remain **unverified and non-executable**. The URL fragment identifies the playground login route; the network environment binds its HTTPS origin. The formerly unspecified `bluemeteor-dev` configuration is now `bluemeteor-test`; operators referencing the old name must update their selections and generate fresh plans.

The owner requested a documentation crawl. The [Supplier Portal category](https://pccdocs.bluemeteor.com/docs/supplier-portal-1) lists nine articles; all nine rendered article bodies were read anonymously in the browser. Direct HTML retrieval returned the page shell with empty article content, so that failed extraction was not used as workflow evidence. [Retrieval coverage](SUPPLIER_PORTAL_SOURCES.json) records visible update dates and extraction limits. This covers that category, not the entire PCC documentation site. Images, videos, downloadable files, distributor-side configuration guides and linked articles outside the category were not independently reviewed. No application login, repository read, model call, submission or API invocation occurred.

## Proposed first three workflows

These are documentation-derived candidates for domain-owner review, not executable skill or oracle approvals. [Qualification cases](SUPPLIER_PORTAL_SCENARIOS.csv) keep every real-environment result `NOT_EXECUTED`.

| Workflow | Documented behavior | Independent review inputs still needed |
|---|---|---|
| SP-W01: template upload and validation | A supplier enabled by a distributor selects an accessible template, uploads a matching Excel file, waits for upload completion, then reviews import results. Header mismatch/order errors reject loading; importing submissions cannot be edited. | Approved category/template version and exact headings; bounded valid/invalid fixtures and expected row counts; independent import/status observer; supplier/template access matrix; cleanup of only this run's submission/items. |
| SP-W02: custom upload and mapping | Option 3 accepts a custom XLSX file. Supplier/Distributor/Both determines who finalizes mapping. Unique identifiers must be mapped; manual field/locale and package mappings are supported. Only mapped/accepted fields are imported; identifier-only mapping may finalize. | Pinned template settings and identifier fields; reviewed source-to-target and locale/package mappings; deterministic confidence samples if testing AI thresholds; comparison of imported fields and omitted columns; cleanup/reconciliation. |
| SP-W03: correct, publish and request edit access | Correct invalid items, revalidate, then publish eligible items. Post-publication editing can require distributor approval or be disallowed. Read-only states, rejection/revalidation and later onboarding depend on configured policies. | Isolated supplier and distributor roles; explicit automation/edit flags; controlled approval/rejection fixtures and immutable baseline fields; bounded polling; independently reviewed expected transitions and compensating procedure. |

W01 follows [upload options](https://pccdocs.bluemeteor.com/docs/3-simple-ways-to-upload-product-data) and the [introduction](https://pccdocs.bluemeteor.com/docs/supplier-portal). W02 follows [supplier-side mapping](https://pccdocs.bluemeteor.com/docs/mapping-packaging-data-in-ai-mapping-finalization-screen-1). W03 follows [open submissions](https://pccdocs.bluemeteor.com/docs/syndication-workflow) and [SKU lifecycle](https://pccdocs.bluemeteor.com/docs/business-process-flow-for-sku-statuses). Their prerequisites must be checked against the actual deployed version.

## Additional coverage and unresolved contracts

The [submission guide](https://pccdocs.bluemeteor.com/docs/syndication) distinguishes submission progress from individual SKU state and restricts edit/upload actions by submission status. The [statistics guide](https://pccdocs.bluemeteor.com/docs/key-statistics) defines aggregate counts and submission/time filters. [Quick Links](https://pccdocs.bluemeteor.com/docs/quick-links) describes help/tutorial/distributor chat entry points; testing chat transmission needs a separate reviewed communication case.

Keep these documentation ambiguities visible in expected-results review:

- The upload article's title lists four methods while parts of its prose still say three. Option 4 requests API enablement and can notify the distributor; it is excluded from initial execution.
- The upload article mentions CSV for an append route, but the new custom-file route specifies XLSX. Test these separately; do not assume identical format support.
- The lifecycle article describes published items as immutable; the newer open-submission article describes configurable edit requests. Pin the configured edit policy and distributor decision before asserting access.
- Newer mapping guidance uses ready-to-accept/queued-for-onboarding labels, while lifecycle and overview pages use PIM Ready/Accepted. Submission, item and display labels need a deployed-version mapping; do not force one global status enum from these documents.
- Mapping finalization is not completion of onboarding. Automatic publication, approval, acceptance and onboarding can bypass visible intermediate stages; polling must have an approved deadline and distinguish unavailable evidence from failure.

The [Supplier Syndications API guide](https://pccdocs.bluemeteor.com/docs/suppliersyndicationsapidoc) documents asynchronous submission/status tracking, separate HTTP/application codes, partial success, per-SKU failures and request identifiers. Its sample API host is `apis-live2.bluemeteor.com`, which is **not an approved test/playground API target** and has not been added to any allowlist. An API pilot needs its own approved non-production base, authentication references, versioned payload contract, quotas and cleanup. The guide also uses inconsistent UOM field spellings and describes a numeric threshold rule differing from the UI's “above” wording. Boundary expectations need owner resolution. A returned callback URL must never grant a new destination.

## Enforced domain approval contract

Environment verification, source-read permission, model-data grants and exact-plan approval remain separate requirements. In addition, the runtime now requires a reviewed domain record before it plans executable organization tests. An existing oracle file alone is insufficient. Missing/currently mismatched domain approval produces the deterministic analysis-only report: zero model calls, zero SUT calls, no approvable execution plan. The synthetic exception requires the actual fixture tracker (and fixture Git provider when selected), a synthetic-labeled story, and a verified synthetic loopback target.

Each record under an operator-managed project's `domain_capabilities` binds one exact tenant/project/repository/story/environment selection, the complete catalog hash, registered action source hashes, expected-results bytes, CSV fixture set and resolved environment contract. It requires an ID/version, owner approval reference, timezone-aware expiry, distinct oracle author/reviewer identities and an independence-evidence reference. These are trusted operator declarations; code cannot prove that humans independently authored or reviewed expected results. Source/model approvals cannot substitute for this record.

The local proposal command has no source/model/SUT client and writes a new file without overwriting an existing review:

```powershell
.\.venv\Scripts\python.exe scripts/domain_proposal.py --project PROJECT_ID --story PROJECT-123 --repository bluemeteorapps/supplierportalui --environment bluemeteor-test --out reports/domain-review/proposal.json
```

The project/story must already be configured and the independent oracle/fixtures must exist locally. The command emits `approval.enabled=false`, `approval.approved=false`, empty reviewer decisions, and inspectable input hashes. Generating a proposal grants nothing. Review the actual source artifacts and domain contracts, complete the human approval/independence fields, then place only the reviewed `approval` object in that project's server configuration. There is no HTTP/model approval path for this operation.

The runtime rechecks domain authorization before model attempts, envelope capture, human review and execution dispatch. The domain receipt is part of the reviewed envelope; expiry, revocation or input drift requires a fresh valid review/plan. Expiry is a phase-entry gate, not a mechanism to undo an already-issued external mutation. Existing caller/lease fencing, job lifetime bounds and resource quarantine still apply. Current registered skills remain synthetic import/mapping implementations; this change does not qualify them for Supplier Portal or implement its login/selectors/API/cleanup.

## Next qualification inputs

Configure the actual Jira project key, scoped source connection and project membership; obtain approved server-side secret references for isolated supplier/distributor accounts. Domain QA must supply the template configuration, versioned expected results, fixture ownership, test-data namespace, cleanup/compensation and deployment-specific status mapping. Platform/security owners must verify real environment isolation, storage/network controls and the IdP. No credential belongs in this document or chat.

Readiness remains **NO-GO** until the implementation ledger's remaining features and external acceptance gates are satisfied. These workflow candidates establish traceable domain requirements; they are not successful live tests or real-model qualification.
