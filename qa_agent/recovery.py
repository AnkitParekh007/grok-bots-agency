"""Offline, bounded encrypted backup/restore. Stop every writer before invoking.

The authenticated stream contains a manifest followed by exactly its file bytes.
Each bounded AES-GCM record is authenticated before plaintext is consumed. A final
authenticated empty record detects truncation; nothing is published before it.
"""
import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .audit_checkpoint import fingerprint
from .store import Store, SCHEMA_VERSION
from .util import canon, now, sha256

MAGIC = b"QA-BUDDY-BACKUP/1\n"
CHUNK = 1_048_576
MAX_BYTES = 2_147_483_648
MAX_FILES = 10000
MAX_MANIFEST = 4_194_304


class RecoveryError(ValueError):
    pass


def _cipher(key, header):
    if not isinstance(key, bytes) or len(key) != 32:
        raise RecoveryError("backup key must be exactly 32 random bytes")
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=header[len(MAGIC):], info=MAGIC).derive(key)
    return AESGCM(derived)


class _Writer:
    def __init__(self, output, key):
        self.output, self.header, self.seq, self.buffer = output, MAGIC + os.urandom(32), 0, bytearray()
        self.aead = _cipher(key, self.header)
        output.write(self.header)

    def _record(self, plain, final=False):
        marker = bytes([int(final)])
        body = self.aead.encrypt(self.seq.to_bytes(12, "big"), plain, self.header + self.seq.to_bytes(8, "big") + marker)
        self.output.write(marker + struct.pack(">I", len(body)) + body)
        self.seq += 1

    def write(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= CHUNK:
            self._record(bytes(self.buffer[:CHUNK]))
            del self.buffer[:CHUNK]

    def finish(self):
        if self.buffer:
            self._record(bytes(self.buffer));self.buffer.clear()
        self._record(b"", final=True)


class _Reader:
    def __init__(self, source, key, limit):
        self.source, self.seq, self.buffer, self.done, self.total, self.limit = source, 0, bytearray(), False, 0, limit
        self.partial = False
        self.header = source.read(len(MAGIC) + 32)
        if len(self.header) != len(MAGIC) + 32 or not self.header.startswith(MAGIC):
            raise RecoveryError("unsupported or truncated backup header")
        self.aead = _cipher(key, self.header)

    def read(self, count):
        if not 0 <= count <= MAX_MANIFEST:
            raise RecoveryError("backup read exceeds bounded allocation")
        while len(self.buffer) < count and not self.done:
            frame = self.source.read(5)
            if len(frame) != 5 or frame[0] not in (0, 1):
                raise RecoveryError("backup is truncated or has an invalid frame")
            size = struct.unpack(">I", frame[1:])[0]
            if not 16 <= size <= CHUNK + 16:
                raise RecoveryError("backup frame exceeds authenticated record bound")
            encrypted = self.source.read(size)
            if len(encrypted) != size:
                raise RecoveryError("backup is truncated")
            try:
                plain = self.aead.decrypt(self.seq.to_bytes(12, "big"), encrypted, self.header + self.seq.to_bytes(8, "big") + frame[:1])
            except InvalidTag:
                raise RecoveryError("backup authentication failed") from None
            self.seq += 1
            if frame[0]:
                if plain or self.source.read(1):
                    raise RecoveryError("backup has an invalid ending or trailing bytes")
                self.done = True
            else:
                if not plain or self.partial:
                    raise RecoveryError("invalid nonfinal backup record sequence")
                self.partial = len(plain) < CHUNK
                self.total += len(plain)
                if self.total > self.limit:
                    raise RecoveryError("backup exceeds restoration byte budget")
                self.buffer.extend(plain)
        out = bytes(self.buffer[:count]);del self.buffer[:count]
        return out


def _name(name):
    if not isinstance(name, str) or len(name.encode()) > 1024 or not name:
        raise RecoveryError("invalid backup member name")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name:
        raise RecoveryError("noncanonical backup member")
    for part in path.parts:
        device = part.split(".", 1)[0].upper()
        if part in (".", "..") or len(part) > 200 or part.endswith((" ", ".")) or any(ord(c) < 32 or c in '<>:"\\|?*' for c in part) or device in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
            raise RecoveryError("unsafe backup member")
    if name not in ("qa_agent.db", "audit.jsonl", "restore-receipt.json") and (len(path.parts) < 2 or path.parts[0] not in ("runs", "audit-recovery")):
        raise RecoveryError("backup member is outside the state allowlist")
    return path


def _hash_file(path, limit=MAX_BYTES + MAX_MANIFEST + CHUNK):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        while chunk := source.read(CHUNK):
            digest.update(chunk);size += len(chunk)
            if size > limit:
                raise RecoveryError("file exceeds backup byte budget")
    return digest.hexdigest(), size


def _inventory(root, snapshot, max_bytes):
    found = {"qa_agent.db": snapshot}
    visited = 0
    for name in ("audit.jsonl", "runs", "audit-recovery", "restore-receipt.json"):
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        pending = [path]
        while pending:
            path = pending.pop()
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.resolve().is_relative_to(root):
                raise RecoveryError("backup refuses links outside isolated state storage")
            if path.is_dir():
                for child in path.iterdir():
                    visited += 1
                    if visited > MAX_FILES:
                        raise RecoveryError("backup exceeds file/directory bound")
                    pending.append(child)
            elif path.is_file() and path.stat().st_nlink == 1:
                found[path.relative_to(root).as_posix()] = path
            else:
                raise RecoveryError("backup refuses special files and hard links")
    manifest, total = [], 0
    for name, path in sorted(found.items()):
        _name(name)
        if path.stat().st_size > max_bytes - total:
            raise RecoveryError("backup exceeds byte budget")
        digest, size = _hash_file(path, max_bytes - total);total += size
        if len(manifest) >= MAX_FILES or total > max_bytes:
            raise RecoveryError("backup exceeds file/byte budget")
        manifest.append({"name": name, "bytes": size, "sha256": digest})
    return manifest, found


def _database(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= version <= SCHEMA_VERSION or db.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
            raise RecoveryError("database integrity or schema compatibility check failed")
        with closing(db.execute("SELECT * FROM audit ORDER BY id")) as cursor:
            ledger = fingerprint(dict(row) for row in cursor)
        return {"schema_version": version, "ledger": ledger}
    finally:
        db.close()


def _discard(path, parent):
    # Deletion is restricted to this tool's generated staging child.
    if path.exists():
        if path.resolve().parent != parent.resolve() or path.is_symlink() or not path.name.startswith(".qa-recovery-"):
            raise RecoveryError("refusing cleanup outside generated recovery staging")
        shutil.rmtree(path)


def backup(data_dir, destination, key, ledger_id, *, confirmed_quiescent=False, max_bytes=MAX_BYTES):
    if confirmed_quiescent is not True or not isinstance(ledger_id, str) or not 1 <= len(ledger_id) <= 200 or not 1024 <= max_bytes <= MAX_BYTES:
        raise RecoveryError("backup requires stopped-writer confirmation, ledger identity and bounded byte budget")
    original = Path(data_dir)
    root = original.resolve()
    target = Path(destination).absolute()
    if original.is_symlink() or not (root / "qa_agent.db").is_file() or (root / "qa_agent.db").is_symlink() or target.resolve().is_relative_to(root) or target.exists() or target.is_symlink():
        raise RecoveryError("backup requires existing isolated state and a new destination outside it")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".qa-recovery-", dir=root))
    os.chmod(stage, 0o700)
    partial = target.parent / (".qa-recovery-" + uuid.uuid4().hex + ".partial")
    started = time.monotonic()
    try:
        with closing(sqlite3.connect(root / "qa_agent.db", isolation_level=None, timeout=5)) as barrier:
            barrier.execute("BEGIN IMMEDIATE")
            active = barrier.execute("SELECT COUNT(*) FROM runs WHERE status IN ('planning','executing')").fetchone()[0]
            running = barrier.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0]
            if active or running:
                raise RecoveryError("backup requires quiescent runs and no running jobs")
            snapshot = stage / "qa_agent.db"
            with closing(sqlite3.connect((root / "qa_agent.db").as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(snapshot)) as copy:
                source.backup(copy)
            state = _database(snapshot)
            files, paths = _inventory(root, snapshot, max_bytes)
            manifest = {"format": "qa-recovery-set/1", "id": uuid.uuid4().hex, "ledger_id": ledger_id,
                        "created_at": now(), "recovery_tool_sha256": sha256(Path(__file__).read_bytes()), **state, "files": files}
            encoded = canon(manifest).encode()
            if len(encoded) > MAX_MANIFEST:
                raise RecoveryError("backup manifest exceeds bound")
            with partial.open("xb") as output:
                os.chmod(partial, 0o600)
                writer = _Writer(output, key)
                writer.write(struct.pack(">I", len(encoded)));writer.write(encoded)
                for item in files:
                    digest, count = hashlib.sha256(), 0
                    with paths[item["name"]].open("rb") as source:
                        while chunk := source.read(CHUNK):
                            writer.write(chunk);digest.update(chunk);count += len(chunk)
                            if count > item["bytes"]:
                                raise RecoveryError("state changed during backup")
                    if count != item["bytes"] or digest.hexdigest() != item["sha256"]:
                        raise RecoveryError("state changed during backup")
                if _inventory(root, snapshot, max_bytes)[0] != files:
                    raise RecoveryError("artifact inventory changed during backup")
                writer.finish();output.flush();os.fsync(output.fileno())
            archive_sha256 = _hash_file(partial)[0]
            # Atomic create without overwriting an existing destination.
            os.link(partial, target);partial.unlink()
            barrier.rollback()
        return {"format": manifest["format"], "id": manifest["id"], "files": len(files), "bytes": sum(f["bytes"] for f in files),
                "schema_version": state["schema_version"], "duration_s": round(time.monotonic() - started, 3), "sha256": archive_sha256}
    finally:
        if partial.exists():
            partial.unlink()
        _discard(stage, root)


def restore(archive, destination, key, ledger_id, *, max_bytes=MAX_BYTES):
    if not isinstance(ledger_id, str) or not 1 <= len(ledger_id) <= 200 or not 1024 <= max_bytes <= MAX_BYTES:
        raise RecoveryError("restore requires expected ledger identity and bounded byte budget")
    original = Path(destination).absolute()
    if original.exists() or original.is_symlink():
        raise RecoveryError("restore destination must not exist; existing state is never overwritten")
    original.parent.mkdir(parents=True, exist_ok=True)
    target = original.parent.resolve() / original.name
    stage = Path(tempfile.mkdtemp(prefix=".qa-recovery-", dir=target.parent))
    os.chmod(stage, 0o700)
    started = time.monotonic()
    try:
        archive = Path(archive)
        if not archive.is_file() or archive.stat().st_size > max_bytes + MAX_MANIFEST + CHUNK:
            raise RecoveryError("encrypted backup is not a bounded regular file")
        with archive.open("rb") as source:
            reader = _Reader(source, key, max_bytes + MAX_MANIFEST + 4)
            length = reader.read(4)
            if len(length) != 4 or not 1 <= (length := struct.unpack(">I", length)[0]) <= MAX_MANIFEST:
                raise RecoveryError("invalid bounded backup manifest")
            try:
                manifest = json.loads(reader.read(length))
            except (ValueError, UnicodeError):
                raise RecoveryError("invalid backup manifest") from None
            if not isinstance(manifest, dict) or manifest.get("format") != "qa-recovery-set/1" or manifest.get("ledger_id") != ledger_id:
                raise RecoveryError("backup format or expected ledger identity differs")
            if not isinstance(manifest.get("id"), str) or len(manifest["id"]) != 32 or any(c not in "0123456789abcdef" for c in manifest["id"]):
                raise RecoveryError("invalid backup identity")
            files = manifest.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
                raise RecoveryError("invalid backup member count")
            names, total = set(), 0
            for item in files:
                if not isinstance(item, dict) or set(item) != {"name", "bytes", "sha256"} or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= max_bytes or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64:
                    raise RecoveryError("invalid backup member metadata")
                _name(item["name"])
                # Also prevent aliases on case-insensitive Windows filesystems.
                alias = item["name"].casefold()
                if alias in names:
                    raise RecoveryError("duplicate or aliased backup member")
                names.add(alias);total += item["bytes"]
            if "qa_agent.db" not in names or total > max_bytes:
                raise RecoveryError("backup database missing or total byte budget exceeded")
            for item in files:
                path = stage.joinpath(*PurePosixPath(item["name"]).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                remaining, digest = item["bytes"], hashlib.sha256()
                with path.open("xb") as output:
                    os.chmod(path, 0o600)
                    while remaining:
                        chunk = reader.read(min(CHUNK, remaining))
                        if not chunk:
                            raise RecoveryError("backup member is truncated")
                        output.write(chunk);digest.update(chunk);remaining -= len(chunk)
                    output.flush();os.fsync(output.fileno())
                if digest.hexdigest() != item["sha256"]:
                    raise RecoveryError("backup member hash differs")
            if reader.read(1) or not reader.done:
                raise RecoveryError("backup has unexpected plaintext or no authenticated ending")
        state = _database(stage / "qa_agent.db")
        if state["schema_version"] != manifest.get("schema_version") or state["ledger"] != manifest.get("ledger"):
            raise RecoveryError("restored database differs from backup receipt")
        receipt = _invalidate_restored_authority(stage, manifest["id"])
        receipt.update(backup_id=manifest["id"], source_schema_version=state["schema_version"], duration_s=round(time.monotonic() - started, 3),
                       ready_for_deployment=False)
        (stage / "restore-receipt.json").write_text(canon(receipt), encoding="utf-8")
        if target.exists() or target.is_symlink():
            raise RecoveryError("restore destination appeared during verification")
        stage.rename(target)
        return receipt
    finally:
        _discard(stage, target.parent)


def _invalidate_restored_authority(stage, backup_id):
    from .jobs import DurableJobs
    store = Store(stage)
    jobs = DurableJobs(store)
    receipt = {"format": "qa-restore-receipt/1", "restored_at": now(), "backup_id": backup_id}
    try:
        with jobs.transaction() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, delete_sql in (("oidc_flows", "DELETE FROM oidc_flows"), ("connection_discovery", "DELETE FROM connection_discovery"), ("connection_scopes", "DELETE FROM connection_scopes")):
                if table in tables:
                    receipt["invalidated_" + table] = db.execute(delete_sql).rowcount
            if "web_sessions" in tables:
                receipt["revoked_sessions"] = db.execute("UPDATE web_sessions SET revoked=? WHERE revoked IS NULL", (time.time(),)).rowcount
            receipt["revoked_eval_grants"] = db.execute("UPDATE eval_grants SET revoked_at=?,revoked_by='restore-maintenance' WHERE revoked_at IS NULL", (now(),)).rowcount
            receipt["invalidated_approvals"] = db.execute(
                "INSERT INTO approvals(run_id,ts,plan_hash,approver,decision,comment,identity,auth_mode) "
                "SELECT a.run_id,?,a.plan_hash,'restore-maintenance','rejected','Backup restored; fresh request and review required',NULL,'restore-maintenance' "
                "FROM approvals a WHERE a.decision='approved' AND a.id=(SELECT MAX(b.id) FROM approvals b WHERE b.run_id=a.run_id)", (now(),)).rowcount
            receipt["blocked_jobs"] = db.execute("UPDATE jobs SET state='blocked',error='backup restored; fresh request and review required',finished=? WHERE state IN ('queued','running')", (time.time(),)).rowcount
            db.execute("UPDATE runs SET status='blocked',error='backup restored; fresh request and review required',updated=? WHERE status IN ('planning','executing','queued_planning','queued_execution','approved','awaiting_approval')", (now(),))
            store.append_audit({"ts": now(), "run_id": None, "tool": "maintenance.restore", "args_hash": sha256(canon(receipt)),
                                "args_summary": canon(receipt), "decision": "allow", "reason": "verified offline restore; prior execution and login authority invalidated",
                                "outcome": "not deployment-qualified; operator review required", "duration_ms": 0})
        return receipt
    finally:
        store.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "restore"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--ledger-id", required=True)
    parser.add_argument("--key-env", default="QA_BACKUP_KEY")
    parser.add_argument("--confirm-quiescent", action="store_true")
    args = parser.parse_args()
    try:
        if not args.key_env.isidentifier() or len(args.key_env) > 128:
            raise RecoveryError("backup credential must be a server-side environment reference")
        from .util import register_secret_env
        register_secret_env(args.key_env)
        value = os.environ.get(args.key_env)
        if not value:
            raise RecoveryError("backup key secret reference is unset")
        key = base64.b64decode(value, validate=True)
        result = backup(args.source, args.destination, key, args.ledger_id, confirmed_quiescent=args.confirm_quiescent) if args.command == "backup" else restore(args.source, args.destination, key, args.ledger_id)
    except (RecoveryError, ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Recovery refused ({type(exc).__name__}); source and existing destination preserved.\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
