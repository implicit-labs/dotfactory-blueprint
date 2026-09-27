"""Journaled local review exports and exact-owned, operator-requested expiry cleanup."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from .evidence_policy import authorization, digest, encoded, expiration
from .ledger import LedgerError
from .linear_evidence import SECRET_PATTERNS as BASE_SECRET_PATTERNS


SECRET_PATTERNS = (
    re.compile(r"(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+"),
    *BASE_SECRET_PATTERNS,
)
SECRET_FIELD = re.compile(r"(?i)(?:.*[_-])?(?:token|password|secret|api[_-]?key|authorization|cookie)\Z")


def _open_directory(path):
    """Walk without following links; callers own the returned directory handle."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.absolute().parts[1:]:
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _relative_parent(root_descriptor, name):
    descriptor = os.dup(root_descriptor)
    try:
        for part in Path(name).parts[:-1]:
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _same_root(root, descriptor, manifest):
    current = _open_directory(root)
    try:
        for info in (os.fstat(descriptor), os.fstat(current)):
            if [info.st_dev, info.st_ino] != manifest["root_identity"]:
                raise LedgerError("evidence directory identity changed; stopped")
    finally:
        os.close(current)


def _write_exclusive(root_descriptor, name, data):
    parent = _relative_parent(root_descriptor, name)
    try:
        descriptor = os.open(Path(name).name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(parent)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _redact(value, fields):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key in fields or SECRET_FIELD.fullmatch(key) else _redact(item, fields)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, fields) for item in value]
    if isinstance(value, str):
        # Removing a matched fragment can leave a quoted/multiword credential behind.
        if any(pattern.search(value) for pattern in SECRET_PATTERNS):
            return "[REDACTED]"
    return value


def _inventory(root):
    if root.is_symlink() or not root.is_dir() or any(p.is_symlink() for p in root.parents):
        raise LedgerError("evidence tree is missing or traverses symlinks")
    files, directories = {}, []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise LedgerError("evidence tree contains a symlink")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            files[relative] = _sha(path.read_bytes())
        else:
            raise LedgerError("evidence tree contains a special file")
    return files, directories


def publish(ledger, execution_id, output, writer):
    snapshot, effective, destination = authorization(ledger, execution_id, output)
    existing = ledger.connection.execute("SELECT * FROM evidence_exports WHERE path=?", (str(destination),)).fetchone()
    if existing:
        if existing["execution_id"] != execution_id:
            raise LedgerError("evidence output belongs to a different run")
        manifest = json.loads(existing["manifest_json"])
        if manifest["effective_digest"] != digest(effective):
            raise LedgerError("evidence policy constraints changed; choose a new output")
        if manifest["expires_at"] and datetime.fromisoformat(manifest["expires_at"]) <= datetime.fromisoformat(ledger.clock().replace("Z", "+00:00")):
            raise LedgerError("evidence export has expired; run evidence-cleanup and choose a new output")
        if existing["status"] not in {"publishing", "published"} or not destination.exists():
            raise LedgerError("evidence export has an incomplete or expired receipt; choose a new output")
        _verify(destination, manifest)
        with ledger.transaction() as db:
            db.execute("UPDATE evidence_exports SET status='published' WHERE id=?", (existing["id"],))
        return _receipt(destination, manifest)
    if destination.exists():
        raise LedgerError("evidence output already exists and is not owned by this export")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after parent creation. No configured root or output may follow a symlink.
    authorization(ledger, execution_id, output)
    staging = Path(tempfile.mkdtemp(prefix=".factory-review-", dir=destination.parent))
    try:
        bundle = staging / "bundle"
        source_receipt = writer(str(bundle))
        packet = json.loads((bundle / "review.json").read_bytes())
        if effective["mode"] == "summary":
            # No title, intent, context, free text, logs, patch, checks or binary artifacts.
            packet = {"schema_version": 1, "execution_id": execution_id,
                      "source": {key: source_receipt[key] for key in ("head_sha", "patch_sha256")},
                      "evidence_mode": "summary", "source_bytes_included": False}
            shutil.rmtree(bundle)  # Exact private staging directory created by this call.
            bundle.mkdir()
        packet = _redact(packet, set(effective["redact_fields"]))
        (bundle / "review.json").write_bytes(encoded(packet) + b"\n")
        files, _ = _inventory(bundle)
        for name in files:
            data = (bundle / name).read_bytes()
            text = data.decode("utf-8", errors="replace")
            if name == "review.json":
                continue
            if any(pattern.search(text) for pattern in SECRET_PATTERNS):
                # Never silently rewrite the source patch/checks or claim unchanged hashes.
                raise LedgerError("review bundle contains a possible secret; use summary mode or repair source")
        files, directories = _inventory(bundle)
        manifest = {"schema_version": 1, "export_id": ledger.id_factory(),
                    "execution_id": execution_id, "project_key": snapshot["project_key"],
                    "policy_digest": snapshot["digest"], "effective_digest": digest(effective),
                    "mode": effective["mode"], "created_at": ledger.clock(),
                    "root_identity": None,
                    "expires_at": expiration(ledger, effective["retention_seconds"]),
                    "head_sha": source_receipt["head_sha"], "patch_sha256": source_receipt["patch_sha256"],
                    "files": files, "directories": directories}
        # Intent is durable before publication; a crash cannot create an unowned final bundle.
        with ledger.transaction() as db:
            db.execute("INSERT INTO evidence_exports VALUES(?,?,?,?,?,?)", (
                manifest["export_id"], execution_id, str(destination), encoded(manifest).decode(),
                "reserving", manifest["expires_at"]))
        ledger._fault("before_evidence_publish")
        parent = _open_directory(destination.parent)
        root_descriptor = None
        try:
            # mkdir is an atomic no-clobber reservation, unlike rename onto an empty directory.
            os.mkdir(destination.name, mode=0o700, dir_fd=parent)
            root_descriptor = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            info = os.fstat(root_descriptor)
            manifest["root_identity"] = [info.st_dev, info.st_ino]
            with ledger.transaction() as db:
                db.execute("UPDATE evidence_exports SET manifest_json=?,status='publishing' WHERE id=?",
                           (encoded(manifest).decode(), manifest["export_id"]))
            for name in sorted(directories, key=lambda v: len(Path(v).parts)):
                relative = _relative_parent(root_descriptor, name)
                try:
                    os.mkdir(Path(name).name, mode=0o700, dir_fd=relative)
                finally:
                    os.close(relative)
            for name, sha in files.items():
                data = (bundle / name).read_bytes()
                if _sha(data) != sha:
                    raise LedgerError("staged evidence changed; publication stopped")
                _same_root(destination, root_descriptor, manifest)
                _write_exclusive(root_descriptor, name, data)
            _write_exclusive(root_descriptor, "evidence-manifest.json", encoded(manifest) + b"\n")
            os.fsync(root_descriptor)
            os.fsync(parent)
            _verify(destination, manifest)
        finally:
            if root_descriptor is not None:
                os.close(root_descriptor)
            os.close(parent)
        ledger._fault("after_evidence_publish")
        with ledger.transaction() as db:
            db.execute("UPDATE evidence_exports SET status='published' WHERE id=?", (manifest["export_id"],))
        return _receipt(destination, manifest)
    finally:
        shutil.rmtree(staging)


def _receipt(destination, manifest):
    return {"output": str(destination), "export_id": manifest["export_id"],
            "head_sha": manifest["head_sha"], "patch_sha256": manifest["patch_sha256"],
            "review_sha256": manifest["files"]["review.json"], "manifest_sha256": _sha(encoded(manifest) + b"\n"),
            "policy_digest": manifest["policy_digest"], "mode": manifest["mode"],
            "expires_at": manifest["expires_at"]}


def _verify(root, manifest, *, deleting=False):
    files, directories = _inventory(root)
    if [root.stat().st_dev, root.stat().st_ino] != manifest["root_identity"]:
        raise LedgerError("evidence directory identity changed; needs operator attention")
    expected = {**manifest["files"], "evidence-manifest.json": _sha(encoded(manifest) + b"\n")}
    if (any(name not in expected or expected[name] != sha for name, sha in files.items())
            or any(name not in manifest["directories"] for name in directories)
            or (not deleting and (files != expected or directories != manifest["directories"]))):
        raise LedgerError("evidence bundle changed; cleanup/export replay needs operator attention")
    return files, directories


def cleanup(ledger, project_key, *, apply=False):
    """Only recorded exported copies expire. Canonical evidence/ledger/workspaces never do."""
    results = []
    rows = ledger.connection.execute("SELECT ee.* FROM evidence_exports ee "
        "JOIN workflow_executions we ON we.id=ee.execution_id JOIN work_items wi ON wi.id=we.work_item_id "
        "WHERE wi.project_key=? AND ee.status<>'deleted' ORDER BY ee.id", (project_key,)).fetchall()
    now = datetime.fromisoformat(ledger.clock().replace("Z", "+00:00"))
    for row in rows:
        expiry = row["expires_at"]
        if not expiry or datetime.fromisoformat(expiry) > now:
            continue
        item = {"export_id": row["id"], "path": row["path"], "status": "eligible"}
        root, manifest = Path(row["path"]), json.loads(row["manifest_json"])
        try:
            deleting = row["status"] == "deleting"
            if not root.exists() and deleting and not root.is_symlink():
                files, directories = {}, []
            else:
                files, directories = _verify(root, manifest, deleting=deleting)
            if apply:
                with ledger.transaction() as db:
                    db.execute("UPDATE evidence_exports SET status='deleting' WHERE id=?", (row["id"],))
                ledger._fault("before_evidence_cleanup")
                if root.exists():
                    descriptor = _open_directory(root)
                    try:
                        _same_root(root, descriptor, manifest)
                        for name, sha in files.items():
                            _same_root(root, descriptor, manifest)
                            parent = _relative_parent(descriptor, name)
                            try:
                                file_descriptor = os.open(Path(name).name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                                with os.fdopen(file_descriptor, "rb") as stream:
                                    if _sha(stream.read()) != sha:
                                        raise LedgerError("evidence changed during cleanup; stopped")
                                os.unlink(Path(name).name, dir_fd=parent)
                            finally:
                                os.close(parent)
                        for name in sorted(directories, key=lambda v: len(Path(v).parts), reverse=True):
                            _same_root(root, descriptor, manifest)
                            parent = _relative_parent(descriptor, name)
                            try:
                                os.rmdir(Path(name).name, dir_fd=parent)
                            finally:
                                os.close(parent)
                        _same_root(root, descriptor, manifest)
                        root.rmdir()
                    finally:
                        os.close(descriptor)
                ledger._fault("after_evidence_cleanup")
                with ledger.transaction() as db:
                    db.execute("UPDATE evidence_exports SET status='deleted' WHERE id=?", (row["id"],))
                item["status"] = "deleted"
        except (LedgerError, OSError) as error:
            item.update(status="needs_attention", reason=str(error) if isinstance(error, LedgerError) else "filesystem operation failed",
                        next_action="Inspect this exact export directory and its journaled manifest; do not delete unowned changes.")
        results.append(item)
    return results
