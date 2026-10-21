"""Recognize native dropped ZIPs by validated content, without changing a study."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from .ingest import InputError
from .offline_archive import MAX_MANIFEST_BYTES, MAX_TOTAL_BYTES, safe_name, unique_object, unpack
from .offline_reviewer import OfflineReviewer
from .review import ReviewError


def inspect_archive(path):
    path = Path(path)
    try:
        if not path.is_file() or path.suffix.lower() != ".zip":
            raise InputError("Choose the original review ZIP, without unzipping it.")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise InputError("This ZIP contains duplicate entries. Ask for the original package.")
            if "manifest.json" in names:
                if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                    raise InputError("The package manifest is too large.")
                manifest = json.loads(archive.read("manifest.json"), object_pairs_hook=unique_object)
                kind = manifest.get("kind") if isinstance(manifest, dict) else None
                if kind not in {"task", "result"}:
                    raise InputError("This ZIP is not a supported review package.")
                if path.stat().st_size > MAX_TOTAL_BYTES:
                    raise InputError("This review package exceeds the supported size limit.")
                manifest, blobs = unpack(path.read_bytes(), kind)
                if kind == "task":
                    OfflineReviewer.validate_manifest(manifest, blobs)
                return {"kind": kind, "package_id": manifest.get("package_id")}
            if "backup_manifest.json" in names:
                if archive.getinfo("backup_manifest.json").file_size > MAX_MANIFEST_BYTES:
                    raise InputError("The backup manifest is too large.")
                manifest = json.loads(archive.read("backup_manifest.json"), object_pairs_hook=unique_object)
                if not isinstance(manifest, dict) or not {"records.db", "workspace.json"} <= manifest.keys():
                    raise InputError("This backup is missing its study files.")
                for name, expected in manifest.items():
                    safe_name(name)
                    if name not in {
                        "records.db",
                        "workspace.json",
                        "desktop-settings.json",
                        "desktop-version.json",
                        "desktop-jobs.sqlite",
                        "desktop-sequence.json",
                        "desktop-current-import.json",
                    } and not name.startswith(
                        ("sources/", "runs/", "exports/", "offline/", "desktop-imports/")
                    ):
                        raise InputError("This backup contains an unsupported study file.")
                    checksum = hashlib.sha256()
                    with archive.open(name) as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            checksum.update(chunk)
                    if checksum.hexdigest() != expected:
                        raise InputError("Backup integrity check failed. Use another verified backup.")
                return {"kind": "backup"}
            raise InputError(
                "This ZIP has no review tasks or study backup. If it is the app installer, unzip it to install the app first."
            )
    except ReviewError as exc:
        raise InputError("This review package could not be validated. " + str(exc)) from exc
    except (
        OSError,
        zipfile.BadZipFile,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        NotImplementedError,
    ) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("This ZIP is damaged or unsupported. Ask for the original package.") from exc
