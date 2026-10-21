"""Editable file associations use staged copies; user source names never change."""

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from .day_grouping import sync_day_units
from .day_review import DayReviewService
from .db import DayReviewUnit, Record, database
from .desktop_inputs import preserve_original
from .ingest import BlobStore, InputError, ingest_batch, inspect_inputs, split_id
from .schemas import Metadata


def import_plan(directory, entries, metadata):
    directory = Path(directory)
    manifest = json.loads((directory / "upload.json").read_text())
    files = manifest["files"]
    if len(entries) != len(files) or {e["file"] for e in entries} != set(files):
        raise InputError("Every uploaded PDF must appear once in the association list.")
    staging = directory / "paired"
    staging.mkdir(exist_ok=True)
    paths, ids = [], set()
    for entry in entries:
        source = entry["source_id"].strip()
        original = manifest.get("attachment_originals", {}).get(entry["file"])
        if original and source != original["source_id"]:
            raise InputError(
                "This attachment belongs to its selected record. Add it again to choose a different record."
            )
        Metadata(record_id=source)
        split_id(source)
        if source in ids:
            raise InputError("Duplicate source ID: " + source)
        ids.add(source)
        target = staging / (source + ".pdf")
        shutil.copyfile(directory / entry["file"], target)
        paths.append(target)
    if any(not row.get("record_id") for row in metadata):
        raise InputError("Choose a scan for every metadata row before checking the import.")
    rows = [
        Metadata.model_validate({k: v or None for k, v in row.items() if k in Metadata.model_fields})
        for row in metadata
    ]
    report = inspect_inputs(paths, rows)
    if report["missing_metadata"]:
        # Missing attachment metadata is valid; main participant is required by ingestion.
        report["warning"] = (
            "Some sources have no metadata. Main records need a participant before AI processing."
        )
    return paths, rows, report


def source_names(directory, entries):
    manifest = json.loads((Path(directory) / "upload.json").read_text())
    names = manifest.get("original_names", {})
    return {e["source_id"].strip(): names[e["file"]] for e in entries if e["file"] in names}


def commit_plan(workspace, directory, entries, metadata):
    paths, rows, report = import_plan(directory, entries, metadata)
    manifest = json.loads((Path(directory) / "upload.json").read_text())
    context = {}
    attachment_details = {}
    for entry in entries:
        original = manifest.get("attachment_originals", {}).get(entry["file"])
        if original:
            path = Path(directory) / original["file"]
            attachment_details[entry["source_id"].strip()] = {
                **preserve_original(workspace, path.read_bytes(), path.suffix),
                **{k: original[k] for k in ("filename", "origin", "added_at")},
                "derived_format": "application/pdf",
            }
    if manifest.get("metadata_file"):
        original = Path(directory) / manifest["metadata_file"]
        context["original_metadata"] = {
            **preserve_original(workspace, original.read_bytes(), original.suffix, "imported-metadata"),
            "filename": manifest.get("metadata_original_name", original.name),
            "record_ids": [m.record_id for m in rows],
        }
    return ingest_batch(
        workspace.factory,
        workspace.store,
        workspace.project_id,
        paths,
        rows,
        actor="desktop-operator",
        source_names=source_names(directory, entries),
        import_context=context,
        reuse_metadata_candidates=True,
        source_details=attachment_details,
    )


def preview_plan(workspace, directory, entries, metadata):
    """Rehearse the exact import and regrouping on a disposable database copy."""
    paths, rows, report = import_plan(directory, entries, metadata)
    with tempfile.TemporaryDirectory(prefix="preview-", dir=directory) as temporary:
        root = Path(temporary)
        with sqlite3.connect(workspace.path / "records.db") as source:
            with sqlite3.connect(root / "records.db") as target:
                source.backup(target)
        factory = database(f"sqlite:///{root / 'records.db'}")
        try:
            sandbox = SimpleNamespace(factory=factory, project_id=workspace.project_id)
            service = DayReviewService(sandbox)
            sync_day_units(service)

            def units():
                with factory() as session:
                    return {
                        u.id: dict(
                            unit_id=u.id,
                            participant=u.subject_id,
                            date=None if u.date.startswith("unknown:") else u.date,
                            records=sorted({m["record_id"] for m in u.snapshot["members"]}),
                            fingerprint=u.fingerprint,
                        )
                        for u in session.scalars(
                            select(DayReviewUnit).where(
                                DayReviewUnit.project_id == workspace.project_id,
                                DayReviewUnit.state != "retired",
                            )
                        )
                    }

            before = units()
            with factory() as session:
                versions = {
                    r.record_id: r.input_version
                    for r in session.scalars(select(Record).where(Record.project_id == workspace.project_id))
                }
            imported = ingest_batch(
                factory,
                BlobStore(root / "sources"),
                workspace.project_id,
                paths,
                rows,
                source_names=source_names(directory, entries),
                reuse_metadata_candidates=True,
            )
            sync_day_units(service)
            after = units()
            report.update(
                versions=versions,
                changed=imported["changed"],
                revised_records=[rid for rid in imported["changed"] if rid in versions],
                unchanged=imported["unchanged"],
                duplicate_content=imported["duplicate_content"],
                affected_reviews=[
                    {k: v for k, v in unit.items() if k != "fingerprint"}
                    for ident, unit in before.items()
                    if after.get(ident, {}).get("fingerprint") != unit["fingerprint"]
                ],
            )
        finally:
            factory.kw["bind"].dispose()
    return report
