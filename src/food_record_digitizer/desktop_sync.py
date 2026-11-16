"""Explicit, preview-first folder synchronization; never runs extraction or deletes records."""

import hashlib
import json
import os
import secrets
from pathlib import Path

from sqlalchemy import select

from .db import Record
from .desktop_inputs import stage_proposals
from .ingest import InputError, current_snapshot, digest


def settings(workspace):
    path = workspace.path / "desktop-sync.json"
    return json.loads(path.read_text()) if path.exists() else {}


def write_settings(workspace, value):
    path = workspace.path / "desktop-sync.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temp, path)


def row_key(row):
    return digest({k: v for k, v in row.items() if k != "record_id"})


def scan(workspace, folder=None, metadata=None, reset_metadata=False):
    saved = settings(workspace)
    raw_folder = folder or saved.get("folder")
    if not raw_folder:
        raise InputError("Choose a sync folder first.")
    folder = Path(raw_folder).expanduser().resolve()
    if not folder.is_dir():
        raise InputError("Sync folder is unavailable. Connect the drive or use Change sync folder.")
    # User input may be in Study/data, but immutable sources and app results are never inputs.
    managed = [
        workspace.path / name
        for name in ("sources", "runs", "exports", "offline", "backups", "desktop-imports")
    ]
    if workspace.path != workspace.root:
        managed.append(workspace.path)
    managed = [p.resolve() for p in managed]
    if any(folder.is_relative_to(p) for p in managed):
        raise InputError("Choose your input folder, not the app-managed sources or results folder.")
    pdfs, metas = [], []
    count, total = 0, 0
    for directory, dirs, files in os.walk(folder, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".")
            and not (base / d).is_symlink()
            and not any((base / d).resolve().is_relative_to(p) for p in managed)
            and not (base / d / "workspace.json").exists()
        )
        if len(base.relative_to(folder).parts) > 32:
            raise InputError("Choose a less deeply nested input folder.")
        for name in sorted(files):
            count += 1
            if count > 10000:
                raise InputError("Choose a smaller input folder (at most 10,000 files).")
            path = base / name
            if name.startswith((".", "~$")) or path.is_symlink() or not path.is_file():
                continue
            # Configuration files in a study root are not metadata.
            if base == workspace.root and name in {
                "workspace.json",
                "desktop-settings.json",
                "desktop-sync.json",
                "desktop-sequence.json",
                "desktop-current-import.json",
            }:
                continue
            suffix = path.suffix.lower()
            if suffix == ".pdf":
                size = path.stat().st_size
                if size > 32 * 1024 * 1024:
                    raise InputError("Each PDF must be at most 32 MB.")
                total += size
                pdfs.append(path)
                if total > 512 * 1024 * 1024 or len(pdfs) > 1000:
                    raise InputError("Sync supports up to 1,000 PDFs / 512 MB. Choose a smaller folder.")
            elif suffix in {".csv", ".xlsx", ".json"}:
                metas.append(path)
    if not pdfs and not metas:
        raise InputError("No PDFs or metadata found. Existing study records have not been changed.")
    same = saved.get("folder") == str(folder)
    chosen = metadata or (saved.get("metadata") if same and not reset_metadata else None)
    if chosen:
        chosen = Path(chosen).resolve()
        if chosen not in metas:
            raise InputError("The saved metadata file is missing. Use Change sync folder to choose it again.")
    elif len(metas) > 1:
        return {"choose_metadata": [str(p) for p in metas], "folder": str(folder)}
    elif metas:
        chosen = metas[0]
    if chosen and chosen.stat().st_size > 4 * 1024 * 1024:
        raise InputError("Metadata must be at most 4 MB.")
    directory = workspace.path / "desktop-imports" / secrets.token_hex(20)
    directory.mkdir(parents=True)
    hints = dict(saved.get("files", {})) if same else {}
    # Also recognize sources first imported through Add records.
    names = {}
    with workspace.factory() as session:
        for record in session.scalars(select(Record).where(Record.project_id == workspace.project_id)):
            for sid, doc in current_snapshot(session, record)["documents"].items():
                if doc.get("original_filename"):
                    names.setdefault(doc["original_filename"], set()).add(sid)
    hints.update(
        {name: next(iter(ids)) for name, ids in names.items() if len(ids) == 1 and name not in hints}
    )
    descriptors, observed = [], {}
    for index, path in enumerate(pdfs):
        payload = path.read_bytes()
        if not payload.startswith(b"%PDF-"):
            raise InputError(f"Invalid PDF: {path.name}")
        local_name = f"file-{index}.pdf"
        (directory / local_name).write_bytes(payload)
        relative = path.relative_to(folder).as_posix()
        descriptors.append({"file": local_name, "filename": relative})
        observed[relative] = hashlib.sha256(payload).hexdigest()
    metadata_path = None
    if chosen:
        metadata_path = directory / ("metadata" + chosen.suffix.lower())
        payload = chosen.read_bytes()
        metadata_path.write_bytes(payload)
        observed[chosen.relative_to(folder).as_posix()] = hashlib.sha256(payload).hexdigest()
    result = stage_proposals(
        workspace,
        directory,
        descriptors,
        metadata_path,
        chosen.name if chosen else None,
        source_hints=hints,
        metadata_hints=saved.get("rows", {}) if same else {},
    )
    result["sync_folder"] = str(folder)
    result["sync_missing_files"] = (
        sorted(set(saved.get("files", {})) - {e["filename"] for e in descriptors}) if same else []
    )
    manifest_path = directory / "upload.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sync"] = {
        "folder": str(folder),
        "metadata": str(chosen) if chosen else None,
        "observed": observed,
    }
    manifest["proposal"] = result
    manifest_path.write_text(json.dumps(manifest))
    write_settings(
        workspace,
        {**(saved if same else {}), "folder": str(folder), "metadata": str(chosen) if chosen else None},
    )
    return result


def check_snapshot(manifest):
    if not (sync := manifest.get("sync")):
        return
    folder = Path(sync["folder"])
    for name, checksum in sync["observed"].items():
        path = folder / name
        if (
            not path.is_file()
            or path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != checksum
        ):
            raise InputError(
                "The sync folder changed after preview. Click Sync folder again before importing."
            )


def remember_import(workspace, manifest, entries, metadata):
    if not (sync := manifest.get("sync")):
        return
    saved = settings(workspace)
    same = saved.get("folder") == sync["folder"]
    links = dict(saved.get("files", {})) if same else {}
    links.update({e["filename"]: e["source_id"] for e in entries})
    rows = dict(saved.get("rows", {})) if same else {}
    # Only exact unambiguous rows can retain their manual association; never infer by row order.
    for row in metadata:
        key = row_key(row)
        ids = set(rows.get(key, []))
        ids.add(row["record_id"])
        rows[key] = sorted(ids)
    write_settings(
        workspace, {"folder": sync["folder"], "metadata": sync["metadata"], "files": links, "rows": rows}
    )
