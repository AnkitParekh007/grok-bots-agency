"""Synthetic, offline recovery drill. Never reads a configured workspace data dir."""
import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from qa_agent.audit_checkpoint import checkpoint, verify
from qa_agent.recovery import backup, restore
from qa_agent.store import Store, FileArtifactStore
from qa_agent.util import canon, now, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("reports/enterprise-readiness/recovery-drill"))
    args = parser.parse_args()
    output = args.out.resolve() / ("synthetic-" + uuid.uuid4().hex)
    output.mkdir(parents=True)
    source = output / "synthetic-source"
    store, art = Store(source), FileArtifactStore(source / "runs")
    expected = {}
    for index in range(8):
        rid = f"synthetic-drill-{index}"
        store.create_run(rid, "SYNTHETIC recovery drill; no model/source/SUT calls", "synthetic-local", "replay", "REPLAYED")
        store.update_run(rid, status="completed")
        content = (f"SYNTHETIC-EVIDENCE-{index}\n".encode() * 131072)
        art.write_bytes(rid, "evidence/drill.txt", content)
        expected[rid] = sha256(content)
        store.append_audit({"ts": now(), "run_id": rid, "tool": "drill.synthetic", "args_hash": sha256(canon({"index": index})),
                            "args_summary": canon({"index": index}), "decision": "allow", "reason": "synthetic recovery evidence",
                            "outcome": "not a real QA execution", "duration_ms": 0})
    signing = Ed25519PrivateKey.generate()
    signed_prefix = checkpoint(store.q("SELECT * FROM audit ORDER BY id"), signing, "synthetic-drill")
    store.db.close()
    key = os.urandom(32)  # Ephemeral drill-only key, never a production key or exported credential.
    created = backup(source, output / "synthetic.qab", key, "synthetic-drill", confirmed_quiescent=True)
    recovered = restore(output / "synthetic.qab", output / "synthetic-restored", key, "synthetic-drill")
    restored = Store(output / "synthetic-restored")
    try:
        observed = {rid: sha256((output / "synthetic-restored/runs" / rid / "evidence/drill.txt").read_bytes()) for rid in expected}
        prefix_ok = verify(signed_prefix, signing.public_key(), "synthetic-drill", restored.q("SELECT * FROM audit ORDER BY id"))
        if observed != expected or not prefix_ok:
            raise RuntimeError("synthetic recovery comparison failed")
        result = {"format": "qa-synthetic-recovery-drill/1", "created_at": now(), "synthetic_only": True,
                  "runs": len(expected), "artifact_hashes_match": observed == expected, "signed_original_audit_prefix_matches": prefix_ok,
                  "backup": created, "restore": recovered, "production_rpo_rto_qualified": False,
                  "limitations": "Single local synthetic sample; no production credentials, outage, supervisor, managed key custody or real-data restoration. Drill encryption key is ephemeral."}
        (output / "drill.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(output / "drill.json"), **result}, indent=2))
    finally:
        restored.db.close()


if __name__ == "__main__":
    main()
