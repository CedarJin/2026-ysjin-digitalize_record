"""Verifiable backups for a stopped SQLite review workspace.

PostgreSQL deployments use pg_dump plus the sources directory (see deployment guide).
Session cookies and pending invitations are invalidated after restore.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from .ingest import InputError


def create_backup(workspace, output):
    if workspace.config["database"] != "sqlite":
        raise InputError(
            "Use pg_dump and a source-directory snapshot for PostgreSQL; see docs/review_deployment.md"
        )
    if output.exists():
        raise InputError("Backup output already exists.")
    with tempfile.TemporaryDirectory() as temporary:
        snapshot = Path(temporary) / "records.db"
        with sqlite3.connect(workspace.path / "records.db") as source, sqlite3.connect(snapshot) as dest:
            source.backup(dest)
        files = {"records.db": snapshot, "workspace.json": workspace.path / "workspace.json"}
        files.update(
            {
                str(p.relative_to(workspace.path)): p
                for folder in ("sources", "runs", "exports", "offline")
                for p in (workspace.path / folder).rglob("*")
                if p.is_file()
            }
        )
        manifest = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as handle, zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, path in files.items():
                archive.write(path, name)
            archive.writestr("backup_manifest.json", json.dumps(manifest, indent=2))
        output.chmod(0o600)
    return {"backup": str(output.resolve()), "files": len(files)}


def restore_backup(source, output):
    if output.exists():
        raise InputError("Restore destination must not exist.")
    with zipfile.ZipFile(source) as archive:
        manifest = json.loads(archive.read("backup_manifest.json"))
        if not {"records.db", "workspace.json"} <= set(manifest):
            raise InputError("Backup is missing required files.")
        for name, expected in manifest.items():
            path = Path(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or name not in {"records.db", "workspace.json"}
                and not name.startswith(("sources/", "runs/", "exports/", "offline/"))
            ):
                raise InputError("Unsafe backup entry.")
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise InputError("Backup checksum failed.")
        output.mkdir(parents=True, mode=0o700)
        try:
            for name in manifest:
                target = output / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
            with sqlite3.connect(output / "records.db") as connection:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise InputError("Restored database failed integrity check.")
                tables = {
                    r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "review_sessions" in tables:
                    connection.execute("DELETE FROM review_sessions")
                if "review_users" in tables:
                    connection.execute("UPDATE review_users SET invite_hash=NULL, invite_expires=NULL")
        except Exception:
            shutil.rmtree(output)
            raise
    return {"workspace": str(output.resolve()), "verified_files": len(manifest), "sessions_revoked": True}
