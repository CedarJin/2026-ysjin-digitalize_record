"""A tidy study container around the backwards-compatible engine workspace.

Relative directory links keep immutable source references and old engine paths
valid while putting user data/results in visible, purpose-named folders.
"""

import json
import os
from pathlib import Path

from .ingest import InputError

LINKS = {
    "sources": "data/sources",
    "runs": "results/extractions",
    "exports": "results/exports",
    "offline": "results/review-packages",
    "backups": "backups",
}


def engine_path(path):
    path = Path(path).resolve()
    manifest = path / "workspace.json"
    if not manifest.exists():
        return path
    config = json.loads(manifest.read_text())
    if config.get("storage_version") == 2:
        if config.get("storage_directory") != ".app":
            raise InputError("Unsupported study storage layout.")
        internal = path / ".app"
        if (
            internal.is_symlink()
            or not (internal / "workspace.json").is_file()
            or not (internal / "records.db").is_file()
        ):
            raise InputError("The study internal folder is missing. Restore a verified backup.")
        return internal
    return path


def study_root(path):
    path = Path(path).resolve()
    if path.name == ".app" and (path.parent / "workspace.json").is_file():
        parent = json.loads((path.parent / "workspace.json").read_text())
        if parent.get("storage_version") == 2 and parent.get("storage_directory") == ".app":
            return path.parent
    return path


def organize(path):
    """Organize a newly created/restored, unopened workspace, never a live study.

    Callers use a fresh destination. Roll back all moves if any step fails.
    The public v2 marker is written last, so old app versions fail closed.
    """
    path = Path(path).resolve()
    original = (path / "workspace.json").read_bytes()
    config = json.loads(original)
    if config.get("storage_version") == 2:
        return path
    if config.get("storage_version") != 1 or config.get("database") != "sqlite":
        raise InputError("Only local SQLite studies can use the organized folder layout.")
    internal = path / ".app"
    if internal.exists() or any((path / p).exists() for p in ["data", "results"]):
        raise InputError("The destination already contains data, results or .app. Choose a new folder.")
    internal.mkdir(mode=0o700)
    moved, links, created = [], [], []
    try:
        for name, destination in LINKS.items():
            target = path / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            source = path / name
            if source.exists():
                if name != destination:
                    os.replace(source, target)
                    moved.append((target, source))
            else:
                target.mkdir(exist_ok=True)
                created.append(target)
            link = internal / name
            link.symlink_to(Path("..") / destination, target_is_directory=True)
            links.append(link)
        for item in list(path.iterdir()):
            if item.name in {"workspace.json", ".app", "data", "results", "backups"}:
                continue
            target = internal / item.name
            os.replace(item, target)
            moved.append((target, item))
        config["organized_layout"] = 1
        (internal / "workspace.json").write_text(json.dumps(config, indent=2))
        public = {
            "storage_version": 2,
            "storage_directory": ".app",
            "project_id": config["project_id"],
            "database": "sqlite",
        }
        temporary = path / ".workspace-layout.tmp"
        temporary.write_text(json.dumps(public, indent=2))
        os.replace(temporary, path / "workspace.json")
    except Exception:
        for link in links:
            link.unlink(missing_ok=True)
        for source, target in reversed(moved):
            os.replace(source, target)
        (internal / "workspace.json").unlink(missing_ok=True)
        for target in reversed(created):
            if target.exists():
                target.rmdir()
        for name in ["data", "results"]:
            parent = path / name
            if parent.exists():
                parent.rmdir()
        internal.rmdir()
        (path / ".workspace-layout.tmp").unlink(missing_ok=True)
        (path / "workspace.json").write_bytes(original)
        raise
    return path
