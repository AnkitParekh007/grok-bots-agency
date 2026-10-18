"""Durable audit projection outbox. SQLite is authoritative; JSONL is rebuildable.

Deliver one sequence per transaction. A crash after fsync but before acknowledgement
is retried idempotently using its exact sequence/hash, without duplicating the line.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .util import canon, now


class AuditProjectionError(ValueError):
    pass


def _tail(path):
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 65536))
        data = stream.read(65536)
    if not data.endswith(b"\n"):
        raise AuditProjectionError("audit projection has an incomplete final line; operator recovery required")
    try:
        item = json.loads(data.splitlines()[-1])
        if not isinstance(item, dict):
            raise ValueError()
        return item
    except (ValueError, IndexError, UnicodeError):
        raise AuditProjectionError("audit projection has an invalid final line") from None


def _append(path, record, *, global_chain=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tail = _tail(path)
    if tail and tail.get("seq") == record["seq"]:
        if tail != record:
            raise AuditProjectionError("audit projection sequence has different content")
        return
    if tail and tail.get("seq") is not None and (type(tail["seq"]) is not int or tail["seq"] >= record["seq"]):
        raise AuditProjectionError("audit projection sequence is ahead of pending delivery")
    if global_chain and tail and tail.get("entry_hash") != record["prev_hash"]:
        raise AuditProjectionError("audit projection head differs from the authoritative ledger")
    with path.open("ab") as stream:
        stream.write((canon(record) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())


class AuditDelivery:
    def __init__(self, store, artifacts, global_path):
        from .store import FileArtifactStore
        # The delivery service may drain another run's ledger receipts. It uses
        # the same constrained root but is independent of a business worker fence.
        self.store, self.artifacts, self.path = store, FileArtifactStore(artifacts.root), Path(global_path)
        if not self.path.resolve().is_relative_to(store.data_dir.resolve()):
            raise ValueError("audit projection outside designated storage")

    def health(self):
        rows = self.store.q("SELECT COUNT(*) AS pending, MIN(created) AS oldest, MAX(attempts) AS attempts "
                            "FROM audit_outbox WHERE delivered IS NULL")
        return rows[0]

    def admission_denial(self, limits=None):
        limits = limits or {}
        state = self.health()
        if state["pending"] >= limits.get("max_pending", 1000):
            return "audit projection backlog exceeded; operator recovery required"
        if state["oldest"]:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["oldest"])).total_seconds()
            if age >= limits.get("max_age_s", 300):
                return "audit projection delivery overdue; operator recovery required"
        return None

    def flush(self, limit=100):
        """No silent overwrite of damaged projections. Return status for health/alerts."""
        delivered = 0
        for _ in range(limit):
            with self.store.lock:
                if self.store.db.in_transaction:
                    return {"delivered": delivered, "deferred": True}
                self.store.db.execute("BEGIN IMMEDIATE")
                row = None
                try:
                    row = self.store.db.execute("SELECT * FROM audit_outbox WHERE delivered IS NULL ORDER BY seq LIMIT 1").fetchone()
                    if not row:
                        self.store.db.commit();break
                    record = json.loads(row["payload"])
                    _append(self.path, record, global_chain=True)
                    if record.get("run_id"):
                        _append(self.artifacts.path_for(record["run_id"], "audit.jsonl"), record)
                    self.store.db.execute("UPDATE audit_outbox SET delivered=?,attempts=attempts+1,last_error=NULL WHERE seq=?",
                                          (now(), row["seq"]))
                    self.store.db.commit()
                    delivered += 1
                except (OSError, ValueError) as error:
                    self.store.db.rollback()
                    if row:
                        self.store.db.execute("UPDATE audit_outbox SET attempts=attempts+1,last_error=? WHERE seq=?",
                                              (type(error).__name__, row["seq"]))
                    return {"delivered": delivered, "error": "audit projection delivery failed", "pending": self.health()["pending"]}
                except Exception:
                    self.store.db.rollback();raise
        return {"delivered": delivered, "pending": self.health()["pending"]}
