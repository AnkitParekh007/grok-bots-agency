"""Compare existing B2 reports offline. Never calls inference or upgrades incomplete evidence."""
import argparse
import glob
import json
from pathlib import Path


def compare(reports):
    rows = []
    for path in reports:
        r = json.loads(Path(path).read_text(encoding="utf-8"))
        a = r.get("aggregate") or {}
        real = (r.get("real_reasoning_evidence") is True and r.get("provider") not in ("stub", "replay")
                and not r.get("simulated_review") and not a.get("contaminated_plans", 0)
                and bool(r.get("runs")) and all((x.get("model_label") or "").startswith("LIVE MODEL (") for x in r["runs"]))
        eligible = real and r.get("verdict") == "PASS" and (r.get("completeness") or {}).get("complete") is True
        rows.append({"source": str(path), "provider": r.get("provider"), "model": r.get("model_id"),
                     "version": r.get("model_version"), "verdict": r.get("verdict", "NOT_EVALUABLE"),
                     "real_reasoning_evidence": real, "quality_claim_eligible": bool(eligible),
                     **{k: a.get(k) for k in ("ac_precision", "ac_recall", "skill_action_correct_rate", "gap_recall",
                         "injection_flagged_rate", "false_alarms", "plan_latency_p95_s", "tokens_total_actual", "cost_usd_actual")}})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("reports", nargs="+")
    ap.add_argument("--out", default="reports/eval/comparison")
    a = ap.parse_args(argv)
    paths = sorted({p for pattern in a.reports for p in glob.glob(pattern)})
    if not paths:
        ap.error("no evaluation reports matched")
    rows = compare(paths)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    columns = ["provider", "model", "version", "verdict", "quality_claim_eligible", "ac_precision", "ac_recall",
               "skill_action_correct_rate", "gap_recall", "injection_flagged_rate", "false_alarms", "plan_latency_p95_s"]
    def cell(value):
        return str(value if value is not None else "unknown").replace("|", "\\|").replace("\n", " ")
    lines = ["# B2 provider comparison", "", "Only complete, uncontaminated real-model PASS reports support quality claims. Recordings remain harness evidence.", "",
             "| " + " | ".join(columns) + " |", "|" + "---|"*len(columns)]
    lines.extend("| " + " | ".join(cell(row[k]) for k in columns) + " |" for row in rows)
    out.with_suffix(".md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
