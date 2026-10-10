import html, json
from .util import canon, now, sha256, redact


def write_report(agent, run_id):
    run = agent.store.get_run(run_id)
    results = agent.store.results(run_id)
    audit = agent.store.audit_rows(run_id)
    rep = {
        "report_version": 1, "generated_at": now(), "run_id": run_id, "story_key": run["story_key"],
        "story_summary": run["story_json"]["fields"]["summary"], "environment": run["environment"],
        "model": {"provider": run["model_provider"], "label": run["model_label"],
                  "note": "Test plan and AC extraction were REPLAYED from a recording; no live LLM was called." if run["model_provider"] == "replay" else ""},
        "oracle_source": agent._oracles(run["story_key"]).get("source"),
        "oracle_review": agent._oracles(run["story_key"]).get("review"),
        "plan_hash": run["plan_hash"],
        "approval": agent.approvals.latest(run_id),
        "acceptance_criteria": run["ac_json"]["acceptance_criteria"],
        "summary": run["summary_json"], "results": results,
        "per_ac": {a["id"]: ac_verdict([r for r in results if a["id"] in r["ac_refs"]]) for a in run["ac_json"]["acceptance_criteria"]},
        "not_run": sum(r["status"] == "not_run" for r in results),
        "run_status_at_report": "cancelled" if agent.is_cancelled(run_id) else run["status"],
        "coverage_gaps": (run.get("planning_json") or {}).get("coverage_gaps", []),
        "understanding": (run.get("planning_json") or {}).get("understanding"),
        "usage": agent.store.run_usage(run_id)["totals"],
        "approval_identity_note": "authenticated local identity" if (agent.approvals.latest(run_id) or {}).get("identity")
                                  else "UNAUTHENTICATED demo approver label (local demo mode)",
        "audit_entries": len(audit), "audit_denied": sum(a["decision"] == "deny" for a in audit),
    }
    rep["artifacts"] = agent.artifacts.manifest(run_id)
    body = redact(json.dumps(rep, indent=2, default=str))
    digest = sha256(body)
    agent.artifacts.write_bytes(run_id, "report.json", body.encode())
    agent.artifacts.write_bytes(run_id, "report.json.sha256", f"{digest}  report.json\n".encode())
    e = html.escape
    rows = "".join(
        f"<tr class='{r['status']}'><td>{e(r['id'])}</td><td>{e(r['title'])}</td><td>{e(','.join(r['ac_refs']))}</td>"
        f"<td><b>{r['status'].upper()}</b></td><td><pre>{e(json.dumps(r['expected']))}</pre></td><td><pre>{e(json.dumps(r['actual']))}</pre></td>"
        f"<td>{e(json.dumps(r['diffs']) if r['diffs'] else (r['error'] or ''))}</td>"
        f"<td>{'<a href=\"artifacts/'+r['screenshot']+'\"><img src=\"artifacts/'+r['screenshot']+'\" width=160></a>' if r.get('screenshot') else ''}"
        f"<br><a href='artifacts/{r['trace']}'>trace</a> · <a href='artifacts/{r['log']}'>log</a></td></tr>" for r in results)
    acs = "".join(f"<tr><td>{a['id']}</td><td>{e(a['text'])}</td><td><i>{e(a['source_quote'])}</i></td><td>{rep['per_ac'][a['id']].upper()}</td></tr>"
                  for a in rep["acceptance_criteria"])
    s = rep["summary"]
    doc = f"""<!doctype html><html><head><meta charset=utf-8><title>QA report {run_id}</title><style>
body{{font-family:sans-serif;margin:2em}} table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ccc;padding:4px;vertical-align:top;font-size:13px}}
tr.failed td,tr.error td{{background:#fde8e8}} tr.passed td{{background:#e8f8ec}} pre{{white-space:pre-wrap;margin:0}} .lbl{{background:#ffe08a;padding:4px 8px;display:inline-block}}
</style></head><body><h1>QA execution report — {e(run['story_key'])}</h1>
<p class=lbl>Model: {e(run['model_label'])}. {e(rep['model']['note'])}</p>
<p>Run <code>{run_id}</code> · env <b>{e(run['environment'])}</b> (synthetic) · generated {rep['generated_at']} UTC</p>
<p>Plan sha256 <code>{run['plan_hash']}</code> approved by <b>{e(str((rep['approval'] or {}).get('approver')))}</b> at {e(str((rep['approval'] or {}).get('ts')))} UTC</p>
<p>Oracle: {e(str(rep['oracle_source']))} — review status <b>{e(str((rep['oracle_review'] or {}).get('status')))}</b></p>
<h2>Summary: {s['passed']} passed / {s['failed']} failed / {s['error']} error / {s['blocked']} blocked of {s['total']}</h2>
<h3>Acceptance criteria traceability</h3><table><tr><th>AC</th><th>Interpretation</th><th>Story quote</th><th>Verdict</th></tr>{acs}</table>
<h3>Test results</h3><table><tr><th>ID</th><th>Test</th><th>AC</th><th>Status</th><th>Expected (oracle)</th><th>Actual</th><th>Mismatch</th><th>Evidence</th></tr>{rows}</table>
<h3>Coverage gaps (not tested)</h3><ul>{''.join(f"<li>{e(','.join(g['ac_refs']) or '-')} [{e(g['kind'])}] {e(g['reason'])}</li>" for g in rep['coverage_gaps']) or '<li>none</li>'}</ul>
<p>Approver identity: {e(rep['approval_identity_note'])}</p>
<p>Model usage: {rep['usage']['calls']} call(s), {rep['usage']['total_tokens']} tokens, cost ${rep['usage']['cost_usd']:.6f} (replay/mock = $0)</p>
<p>Not run (cancelled): {rep['not_run']}</p>
<p>Audit: {rep['audit_entries']} tool calls logged ({rep['audit_denied']} denied) — <a href="audit.jsonl">audit.jsonl</a></p>
<p>report.json sha256: <code>{digest}</code></p></body></html>"""
    agent.artifacts.write_bytes(run_id, "report.html", redact(doc).encode())
    return {"sha256": digest}


def ac_verdict(rs):
    if not rs:
        return "untested (coverage gap)"
    st = {r["status"] for r in rs}
    for v in ("failed", "error", "blocked", "not_run"):
        if v in st:
            return v
    return "passed"
