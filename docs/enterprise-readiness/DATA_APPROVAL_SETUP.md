# Source classification and model-data approval

No organization data has been authorized for inference by supplying a Jira site or Bitbucket workspace URL. Existing provider defaults remain synthetic-only. The implementation requires explicit operator-managed records of data-owner decisions before real source content enters a model, including a local model. These records are declarations to be verified during governance/live qualification; they do not prove a provider's retention, training use or actual processing location.

For each application project, configure `source_policy` with `approved`, `approved_by`, `approval_ref`, a future timezone-aware `expires_at`, the exact `tenant_id`, `classification`, and an explicit `fields` list. Supported declared classes are `public`, `internal`, `confidential`, and `restricted`. `synthetic` cannot be assigned to a real organization source by this policy. Include only approved/minimized fields. The current planner needs `summary` and `description`; a configured custom acceptance-criteria field must also be explicitly approved.

```json
{
  "source_policy": {
    "approved": false,
    "approved_by": null,
    "approval_ref": null,
    "expires_at": null,
    "tenant_id": "ORGANIZATION_ID",
    "classification": null,
    "fields": ["summary", "description"],
    "acceptance_criteria_field": null
  }
}
```

Add a separate entry to the root configuration's `model_data_grants` list for each exact approved combination. The selected provider must independently allow the data class in `allowed_data_classes`, and its configuration must declare a matching `region`. No grant is inferred from a model being local, available or free. Multiple matching grants are rejected as ambiguous.

```json
{
  "model_data_grants": [{
    "approved": false,
    "approved_by": null,
    "approval_ref": null,
    "expires_at": null,
    "tenant_id": "ORGANIZATION_ID",
    "project": "APPLICATION_PROJECT_KEY",
    "provider": "ollama",
    "model": "EXACT_APPROVED_MODEL_ID",
    "endpoint": "http://127.0.0.1:11434",
    "purpose": "plan_story",
    "data_classes": [],
    "region": null,
    "retention_decision": null,
    "training_use_decision": null
  }]
}
```

The examples deliberately grant nothing. The owner must supply the actual project, field names, data decisions, model identity and references; no credentials belong in either record. Use server-side secret references for connection/model credentials as documented in [connection setup](CONNECTION_SETUP.md).

The source gate runs before native model preparation. It minimizes issue fields, captures an observed typed source bundle and issues a short-lived internal permit bound to the run, project/tenant, model/endpoint/purpose and actual prompt inputs. The provider checks the permit again before each request attempt. Changed content, classification, source policy, grant or run scope is rejected. Permit values are excluded from model messages and audit records. Denials are recorded; approval permits a model request, never test execution.

The source bundle includes immutable content hashes, field locations, source classification, completeness limits and authoritative requirement IDs. When a custom AC field is configured, model output must quote that field's own requirement and cite its matching source ID/hash. A quote from the description cannot substitute for it. Upstream revision is explicitly unknown when unavailable; observed-content hashes are not evidence of Jira version completeness.

The observed bundle and model output are retained with the run and bound to the execution envelope. Source snapshot tampering or source/configuration changes invalidate execution approval. Corporate repository/comment/attachment grounding, independently verified upstream versions, secret/DLP handling and actual region/retention enforcement remain tracked acceptance work; an issue-field bundle does not imply those sources were read.

Stories missing independent execution oracles take the separate deterministic analysis-only path. That path makes zero model/test calls and does not bypass these grants. Existing recordings remain synthetic harness evidence and are refused as a substitute for organization-model inference.
