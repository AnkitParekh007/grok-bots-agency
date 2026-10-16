"""Non-executable source review when independent execution inputs are absent.

This extracts source text deterministically. It makes no reasoning, test-result or
coverage-quality claim and never calls a model or resolves an execution target.
"""
import html
import json

from .integrations.common import extract_acceptance_criteria, adf_text
from .util import canon, sha256, now


def source_review(agent, run_id, story_key, *, reason, story=None):
    run = agent.store.get_run(run_id)
    project = run.get("project")
    if project:
        agent.select(project, story_key, run.get("repository"), run.get("environment"))
    tracker = agent.tracker_for(project)
    if story is None:
        story = agent._tool(run_id, "tracker.get_issue", {"key": story_key}, lambda: tracker.get_issue(story_key))
    serialized = canon(story).encode()
    if len(serialized) > 2_000_000:
        raise ValueError("source review exceeds story byte limit")
    fields = story.get("fields") or {}
    custom = getattr(tracker, "ac_field", None)
    criteria = extract_acceptance_criteria(fields, custom)
    sources = {}
    for ac in criteria:
        field = ac["source"]
        text = adf_text(fields.get(field))
        if ac["text"] not in text:
            raise ValueError("extracted criterion has no verbatim source evidence")
        source_id = f"jira:{story_key}:field:{field}"
        start = text.index(ac["text"])
        ac.update(source_quote=ac["text"], source_id=source_id,
                  source_sha256=sha256(text), line_start=text[:start].count("\n") + 1,
                  line_end=text[:start + len(ac["text"])].count("\n") + 1)
        sources[source_id] = {"field": field, "sha256": sha256(text), "text": text}
    gaps = [{"ac_refs": [ac["id"]], "kind": "missing_execution_capability", "reason": reason} for ac in criteria]
    if not criteria:
        gaps.append({"ac_refs": [], "kind": "source_review_required",
                     "reason": "No recognized acceptance-criteria section was found; a human must identify the requirements."})
    review = {"format": "qa-source-review/1", "generated_at": now(), "mode": "analysis_only",
              "method": "Deterministic source extraction; no model inference", "execution_eligible": False,
              "quality_claim_eligible": False, "reason": reason, "story_key": story_key,
              "story_summary": str(fields.get("summary", "")), "source_sha256": sha256(serialized),
              "scope": {k: run.get(k) for k in ("tenant_id", "project", "requested_by")},
              "acceptance_criteria": criteria, "coverage_gaps": gaps,
              "source_fields": {k: {p: v for p, v in source.items() if p != "text"} for k, source in sources.items()},
              "tests_executed": 0, "model_calls": 0,
              "required_before_execution": ["Reviewed domain actions and parameter contracts", "Approved environment and fixtures",
                                            "Independently authored and reviewed expected results", "Data classification and model authorization",
                                            "A newly generated validated plan and distinct-human execution approval"]}
    review["review_sha256"] = sha256(canon(review))
    agent.artifacts.write_bytes(run_id, "inputs/source.json", serialized)
    agent.artifacts.write_bytes(run_id, "inputs/source-fields.json", canon(sources).encode())
    agent.artifacts.write_bytes(run_id, "report.json", json.dumps(review, indent=2).encode())
    e = html.escape
    items = "".join(f"<li><b>{e(ac['id'])}</b> {e(ac['text'])}<p>Source: {e(ac['source_id'])}, lines {ac['line_start']}–{ac['line_end']}</p></li>" for ac in criteria)
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Source review — {e(story_key)}</title><style>body{{font:16px/1.6 system-ui;margin:3em auto;max-width:70ch;padding:0 1em}}li{{margin:1em 0}}p{{overflow-wrap:anywhere}}</style>
<h1>Source review: {e(story_key)}</h1><p>{e(review['story_summary'])}</p><p><b>Analysis only. Execution is unavailable.</b></p>
<p>{e(reason)}</p><p>{e(review['method'])}. Zero tests executed. This report does not establish acceptance-criteria completeness or model quality.</p>
<h2>Extracted source criteria</h2><ol>{items or '<li>Human requirement identification is required.</li>'}</ol>
<h2>Required before execution</h2><ul>{''.join('<li>' + e(item) + '</li>' for item in review['required_before_execution'])}</ul>
<p>Source SHA-256: {e(review['source_sha256'])}</p><p>Review SHA-256: {e(review['review_sha256'])}</p></html>"""
    agent.artifacts.write_bytes(run_id, "report.html", document.encode())
    agent.store.update_run(run_id, story_key=story_key, story_json=story, analysis_json=review,
                           ac_json={"acceptance_criteria": criteria, "gaps": [g["reason"] for g in gaps]},
                           model_label="Source extraction (no model inference)", status="analysis_only")
    agent.gateway.record(run_id, "source.analysis", {"story": story_key, "source_sha256": review["source_sha256"]},
                         "allow", "non-executable source review; no model or SUT call", "analysis_only")
    agent.store.event(run_id, "analysis", reason + " Source review saved; execution is unavailable.")
    return review
