"""Bounded evidence publication. Native capture buffers and disk quotas are separate.

Runtime writers serialize admission and atomic replacement with the shared SQLite
write barrier. Filesystem ACLs must exclude untrusted/out-of-band writers.
"""
import hashlib
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .interfaces import ArtifactStore

DEFAULTS = {"max_file_bytes": 20_000_000, "max_run_bytes": 100_000_000,
            "max_total_bytes": 1_000_000_000, "max_run_files": 500,
            "max_total_files": 10000, "minimum_free_bytes": 100_000_000,
            "inventory_timeout_s": 5}


class StorageLimitExceeded(RuntimeError):
    pass


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StorageLimitExceeded("evidence inventory contains a link or special file")
    return info


def _directory(path):
    if path.is_symlink() or path.is_junction() or not path.is_dir():
        raise StorageLimitExceeded("evidence inventory contains an unsafe directory")


class FileArtifactStore(ArtifactStore):
    def __init__(self, runs_dir, guard=None, *, store=None, limits=None):
        self.root = Path(runs_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.stage = self.root.parent / "artifact-staging"
        self.guard, self.store = guard, store
        self.lock = store.lock if store is not None else threading.RLock()
        limits = {} if limits is None else limits
        if not isinstance(limits, dict) or set(limits) - set(DEFAULTS):
            raise ValueError("unknown evidence storage limit")
        self.limits = {**DEFAULTS, **limits}
        if any(type(value) is not int or value <= 0 for value in self.limits.values()):
            raise ValueError("evidence storage limits must be positive integers")
        p = self.limits
        if not (p["max_file_bytes"] <= 64_000_000 and p["max_file_bytes"] <= p["max_run_bytes"] <= p["max_total_bytes"] <= 1_000_000_000_000
                and p["max_run_files"] <= p["max_total_files"] <= 100000
                and 1_000_000 <= p["minimum_free_bytes"] <= 1_000_000_000_000 and p["inventory_timeout_s"] <= 30):
            raise ValueError("invalid evidence storage bounds")

    def _guard(self, run_id):
        if self.guard:
            self.guard(run_id, allow_cancel=True, authorize=False)

    def path_for(self, run_id, rel="", *, create=True):
        self._guard(run_id)
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("invalid run artifact identity")
        relative = Path(rel)
        if relative.is_absolute() or any(p.startswith(".") for p in relative.parts):
            raise ValueError("path escapes run directory or names private staging")
        base = self.root / run_id
        path = base / relative
        if base.resolve().parent != self.root or not path.resolve().is_relative_to(base.resolve()):
            raise ValueError("path escapes run directory")
        for item in (base, *path.parents):
            if item == self.root.parent:
                break
            if item.exists() or item.is_symlink() or item.is_junction():
                _directory(item)
        if path.is_symlink():
            raise ValueError("artifact path is a link")
        if path.is_file():
            _regular(path)
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @contextmanager
    def _writer(self, run_id, *, fence=True):
        with self.lock:
            outer = self.store is not None and self.store.db.in_transaction
            if self.store is not None and not outer:
                self.store.db.execute("BEGIN IMMEDIATE")
            try:
                if fence:
                    self._guard(run_id)
                yield
                if self.store is not None and not outer:
                    self.store.db.commit()
            except BaseException:
                if self.store is not None and not outer:
                    self.store.db.rollback()
                raise

    def _inventory(self):
        """Bound traversal even for hostile empty-directory trees and staging remnants."""
        deadline = time.monotonic() + self.limits["inventory_timeout_s"]
        files, visited = {}, 0
        for root in (self.root, self.stage):
            if not root.exists() and not root.is_symlink() and not root.is_junction():
                continue
            _directory(root)
            pending = [root]
            while pending:
                directory = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > self.limits["max_total_files"] * 4 + 128 or time.monotonic() > deadline:
                            raise StorageLimitExceeded("evidence inventory traversal limit exceeded")
                        path = Path(entry.path)
                        if entry.is_dir(follow_symlinks=False):
                            _directory(path)
                            pending.append(path)
                            continue
                        info = _regular(path)
                        run_id = (path.relative_to(self.root).parts[0] if root == self.root
                                  else path.name.split(".", 1)[0])
                        files[path] = (run_id, info.st_size)
                        if len(files) > self.limits["max_total_files"] + 1:
                            raise StorageLimitExceeded("total evidence file count exceeded")
        if time.monotonic() > deadline:
            raise StorageLimitExceeded("evidence inventory traversal limit exceeded")
        return files

    def _capacity(self, files, *, target=None, size=0, run_id=None, retiring=None, admission=False):
        counts, sizes, total = {}, {}, 0
        for path, (owner, length) in files.items():
            if path in (target, retiring):
                continue
            if length > self.limits["max_file_bytes"]:
                raise StorageLimitExceeded("existing evidence file exceeds its byte limit")
            total += length
            counts[owner] = counts.get(owner, 0) + 1
            sizes[owner] = sizes.get(owner, 0) + length
        if target is not None:
            if size > self.limits["max_file_bytes"]:
                raise StorageLimitExceeded("evidence file byte limit exceeded")
            total += size
            counts[run_id] = counts.get(run_id, 0) + 1
            sizes[run_id] = sizes.get(run_id, 0) + size
        total_files = sum(counts.values())
        if total > self.limits["max_total_bytes"] or total_files > self.limits["max_total_files"]:
            raise StorageLimitExceeded("total evidence capacity exceeded")
        if any(n > self.limits["max_run_files"] for n in counts.values()) or any(n > self.limits["max_run_bytes"] for n in sizes.values()):
            raise StorageLimitExceeded("per-run evidence capacity exceeded")
        if admission and (total == self.limits["max_total_bytes"] or total_files == self.limits["max_total_files"]
                          or (run_id and (counts.get(run_id, 0) == self.limits["max_run_files"] or sizes.get(run_id, 0) == self.limits["max_run_bytes"]))):
            raise StorageLimitExceeded("evidence capacity is full; operator review required")
        free = shutil.disk_usage(self.root).free
        if free - size < self.limits["minimum_free_bytes"]:
            raise StorageLimitExceeded("evidence write would consume the disk reserve")
        return {"artifact_bytes": total, "artifact_files": total_files,
                "artifact_staging_files": sum(path.is_relative_to(self.stage) for path in files)}

    def ensure_capacity(self, run_id=None):
        with self._writer(run_id, fence=run_id is not None):
            return self._capacity(self._inventory(), run_id=run_id, admission=True)

    def check_execution_capacity(self, run_id, plan):
        """Reject a known impossible evidence-file budget before approving mutation.

        This is a preflight, not a byte reservation or a promise about future space.
        """
        cases = plan.get("test_cases") if isinstance(plan, dict) else None
        if not isinstance(cases, list) or len(cases) > 50:
            raise ValueError("evidence preflight requires a bounded validated test plan")
        relatives = ["report.json", "report.json.sha256", "report.html"]
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("id"), str):
                raise ValueError("evidence preflight requires test identities")
            relatives.extend(f"evidence/{case['id']}/{name}" for name in ("screenshot.png", "trace.zip", "log.txt"))
        with self._writer(run_id):
            files = self._inventory()
            for relative in relatives:
                path = self.path_for(run_id, relative, create=False)
                files.setdefault(path, (run_id, 0))
            return self._capacity(files, run_id=run_id)

    def write_bytes(self, run_id, rel, data):
        return self._publish(run_id, rel, data)

    def _publish(self, run_id, rel, data, *, retiring=None):
        if not isinstance(data, bytes):
            raise TypeError("evidence publication requires immutable bytes")
        if len(data) > self.limits["max_file_bytes"]:
            raise StorageLimitExceeded("evidence file byte limit exceeded")
        target = self.path_for(run_id, rel, create=False)
        partial = None
        with self._writer(run_id):
            files = self._inventory()
            if retiring is not None and (retiring.parent != self.stage or not retiring.name.startswith(run_id + ".")):
                raise StorageLimitExceeded("capture retirement is outside its run scope")
            self._capacity(files, target=target, size=len(data), run_id=run_id, retiring=retiring)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd, name = tempfile.mkstemp(prefix=".artifact-", dir=target.parent)
                partial = Path(name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._guard(run_id)
                # Revalidate paths after writing, before replacing published bytes.
                if self.path_for(run_id, rel, create=False) != target:
                    raise StorageLimitExceeded("artifact destination changed")
                if target.exists():
                    _regular(target)
                _regular(partial)
                os.replace(partial, target)
            finally:
                if partial is not None:
                    partial.unlink(missing_ok=True)  # Only this call's exclusively created temporary file.
        return str(target)

    def capture_file(self, run_id, rel, producer):
        """Native local producer staging; cap publication, not the producer's disk writes."""
        self.path_for(run_id, rel, create=False)
        with self._writer(run_id):
            self._capacity(self._inventory(), run_id=run_id, admission=True)
            self.stage.mkdir(parents=True, exist_ok=True)
            _directory(self.stage)
            fd, name = tempfile.mkstemp(prefix=run_id + ".", suffix=".part", dir=self.stage)
            os.close(fd)
        path = Path(name)
        try:
            producer(path)
            info = _regular(path)
            if not 0 < info.st_size <= self.limits["max_file_bytes"]:
                raise StorageLimitExceeded("native evidence exceeds its publication byte limit")
            with path.open("rb") as stream:
                data = stream.read(self.limits["max_file_bytes"] + 1)
            if len(data) != info.st_size:
                raise StorageLimitExceeded("native evidence changed during capture")
            return self._publish(run_id, rel, data, retiring=path)
        finally:
            # Cleanup is allowed after caller/lease revocation, but serializes
            # with inventory/publication in every cooperating runtime instance.
            with self._writer(None, fence=False):
                path.unlink(missing_ok=True)  # Only this call's exclusive capture.

    def manifest(self, run_id):
        base = self.path_for(run_id, create=False)
        out = []
        with self._writer(run_id):
            files = self._inventory()
            deadline = time.monotonic() + self.limits["inventory_timeout_s"]
            for path, (owner, size) in sorted(files.items()):
                if owner != run_id or not path.is_relative_to(base) or path.name == "manifest.json" or path.name.startswith(".artifact-"):
                    continue
                if size > self.limits["max_file_bytes"]:
                    raise StorageLimitExceeded("manifest evidence file exceeds its byte limit")
                digest, observed = hashlib.sha256(), 0
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        observed += len(chunk)
                        if observed > self.limits["max_file_bytes"] or time.monotonic() > deadline:
                            raise StorageLimitExceeded("manifest hashing exceeds its byte/time limit")
                        digest.update(chunk)
                if observed != size:
                    raise StorageLimitExceeded("manifest evidence changed during hashing")
                out.append({"path": str(path.relative_to(base)), "bytes": observed, "sha256": digest.hexdigest()})
        return out
