"""Typed observed source snapshots with verbatim requirement evidence.

Content hashes identify the observed version; upstream version is explicit when
available. A hash alone does not prove the upstream project is complete/current.
"""
from .integrations.common import adf_text, extract_acceptance_criteria
from .util import sha256, canon
from .providers.models import DataClassDenied


def issue_bundle(story, rule, custom_field=None):
    fields = story.get("fields") or {}
    custom_field = custom_field or rule.get("acceptance_criteria_field")
    if custom_field and custom_field not in rule["fields"]:
        raise DataClassDenied("configured acceptance-criteria field is not approved for model input")
    sources, ids = [], {}
    for field in rule["fields"]:
        if field not in fields or field in ("summary",):
            continue
        value = fields[field]
        if not isinstance(value, (str, dict, list)):
            raise DataClassDenied("unsupported approved source field type")
        text = adf_text(value)
        if len(text.encode()) > 100_000:
            raise DataClassDenied("approved source field exceeds input byte limit; source review required")
        digest = sha256(text)
        source_id = f"jira:{story['key']}:field:{field}:{digest}"
        ids[field] = source_id
        sources.append({"id": source_id, "kind": "jira_field", "field": field, "content": text,
                        "sha256": digest, "classification": rule["classification"], "complete": True,
                        "upstream_version": None, "version_basis": "observed-content-sha256"})
    requirements = extract_acceptance_criteria(fields, custom_field)
    if not requirements:
        raise DataClassDenied("approved sources have no recognized requirements; human source review required")
    for ac in requirements:
        field = ac.pop("source")
        source = next(s for s in sources if s["id"] == ids[field])
        offset = source["content"].find(ac["text"])
        if offset < 0:
            raise DataClassDenied("requirement is not verbatim in its approved source")
        ac.update(source_id=source["id"], source_sha256=source["sha256"],
                  line_start=source["content"][:offset].count("\n") + 1,
                  line_end=source["content"][:offset + len(ac["text"])].count("\n") + 1)
    bundle = {"format": "qa-source-bundle/1", "sources": sources, "requirements": requirements,
              "classification": rule["classification"], "classification_approval": rule["approval_ref"],
              "completeness": "approved issue fields only; other sources have not been ingested"}
    if len(canon(bundle).encode()) > 300_000:
        raise DataClassDenied("source bundle exceeds input limit")
    return bundle
