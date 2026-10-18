"""Operator-held Ed25519 audit checkpoints and offline projection recovery.

Keep signed checkpoints and their pinned public key in independent storage. A local
signature alone is not an immutable external copy or proof that the signing host is safe.
"""
import argparse
import base64
import hashlib
import itertools
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .store import Store, FileArtifactStore, verify_chain
from .util import canon, now

FIELDS = (*Store.AUDIT_FIELDS, "prev_hash", "entry_hash", "seq", "payload_hash")


def fingerprint(rows):
    digest, count = hashlib.sha256(), 0
    def scanned():
        nonlocal count
        for row in rows:
            item = {key: row.get(key) for key in FIELDS}
            digest.update((canon(item) + "\n").encode())
            count += 1
            yield row
    chain = verify_chain(scanned())
    if not chain["ok"]:
        raise ValueError("audit chain is invalid; checkpoint refused")
    return {"count": count, "head": chain.get("head"), "last_seq": chain.get("last_seq"),
            "body_sha256": digest.hexdigest()}


def checkpoint(rows, key, ledger_id):
    if not isinstance(ledger_id, str) or not 1 <= len(ledger_id) <= 200:
        raise ValueError("explicit ledger identity is required")
    payload = {"format": "qa-audit-checkpoint/1", "ledger_id": ledger_id, "created_at": now(), **fingerprint(rows)}
    return {"payload": payload, "signature": base64.b64encode(key.sign(canon(payload).encode())).decode()}


def verify(receipt, public_key, ledger_id, rows=None):
    try:
        payload = receipt["payload"]
        if payload["format"] != "qa-audit-checkpoint/1" or payload["ledger_id"] != ledger_id or type(payload["count"]) is not int or payload["count"] < 0:
            raise ValueError()
        public_key.verify(base64.b64decode(receipt["signature"], validate=True), canon(payload).encode())
        if rows is not None:
            observed = fingerprint(itertools.islice(rows, payload["count"]))
            if any(observed[k] != payload[k] for k in observed):
                raise ValueError()
        return True
    except (ValueError, KeyError, TypeError, InvalidSignature):
        return False


def open_ledger(data_dir, *, writable=False):
    path = Path(data_dir).resolve() / "qa_agent.db"
    db = sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def records(db):
    return (dict(row) for row in db.execute("SELECT * FROM audit ORDER BY id"))


def repair_projections(data_dir):
    """Explicit operator action: preserve old bytes, then rebuild from a verified ledger.

    Reject active runs and hold the SQLite write lock while replacing projections.
    No Store constructor, inference, job recovery or test execution is involved.
    """
    data_dir = Path(data_dir).resolve()
    artifacts = FileArtifactStore(data_dir / "runs")
    recovery = data_dir / "audit-recovery" / uuid.uuid4().hex
    if not recovery.resolve().is_relative_to(data_dir):
        raise ValueError("audit recovery directory escapes the designated data directory")
    with open_ledger(data_dir, writable=True) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('planning','executing')").fetchone()[0]:
                raise ValueError("projection repair requires quiescent planning/execution")
            summary = fingerprint(records(db))
            runs = [row[0] for row in db.execute("SELECT DISTINCT run_id FROM audit WHERE run_id IS NOT NULL")]
            if len(runs) > 10000:
                raise ValueError("projection recovery exceeds run-file bound; use an approved archival recovery procedure")
            destinations = {None: data_dir / "audit.jsonl", **{rid: artifacts.path_for(rid, "audit.jsonl") for rid in runs}}
            if any(not path.resolve().is_relative_to(data_dir) for path in destinations.values()):
                raise ValueError("audit projection escapes the designated data directory")
            recovery.mkdir(parents=True)
            staging = {}
            for index, (rid, path) in enumerate(destinations.items()):
                if path.exists():
                    shutil.copyfile(path, recovery / f"{index}.before.jsonl")
                temp = recovery / f"{index}.rebuilt.jsonl"
                temp.touch()
                staging[rid] = temp
            for row in records(db):
                line = (canon({key: row.get(key) for key in FIELDS}) + "\n").encode()
                for rid in ((None, row["run_id"]) if row.get("run_id") else (None,)):
                    with staging[rid].open("ab") as output:
                        output.write(line)
            for rid, temp in staging.items():
                with temp.open("ab") as output:
                    output.flush();os.fsync(output.fileno())
                destination = destinations[rid]
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Same-volume atomic replacement; preserved originals remain in recovery.
                os.replace(temp, destination)
            db.execute("UPDATE audit_outbox SET delivered=?,last_error=NULL WHERE delivered IS NULL", (now(),))
            db.commit()
            receipt = {"format": "qa-audit-projection-repair/1", "created_at": now(), "ledger": summary,
                       "projections": {str(index): str(path.relative_to(data_dir)) for index, path in enumerate(destinations.values())}}
            (recovery / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            return receipt
        except Exception:
            db.rollback();raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["checkpoint", "verify", "repair-projections"])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--ledger-id")
    parser.add_argument("--key-env", default="QA_AUDIT_SIGNING_KEY")
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.command == "repair-projections":
        print(json.dumps(repair_projections(args.data_dir), indent=2));return
    if not args.ledger_id:
        parser.error("--ledger-id is required")
    with open_ledger(args.data_dir) as db:
        db.execute("BEGIN")
        if args.command == "checkpoint":
            encoded = os.environ.get(args.key_env)
            if not encoded:
                parser.error("signing key secret reference is unset")
            key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(encoded, validate=True))
            print(json.dumps(checkpoint(records(db), key, args.ledger_id), indent=2))
        else:
            if not args.public_key or not args.receipt:
                parser.error("--public-key and --receipt are required")
            public = Ed25519PublicKey.from_public_bytes(base64.b64decode(args.public_key.read_text().strip(), validate=True))
            if not verify(json.loads(args.receipt.read_text()), public, args.ledger_id, records(db)):
                parser.error("checkpoint signature, identity or ledger prefix does not match")
            print("Checkpoint signature and ledger prefix verified.")


if __name__ == "__main__":
    main()
