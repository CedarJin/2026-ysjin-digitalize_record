"""Friendly metadata and durable ID proposals for graphical imports."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select

from .db import Record
from .ingest import InputError, current_snapshot
from .schemas import Metadata

ALIASES = {
    "participant": "subject_id",
    "participant_id": "subject_id",
    "participantid": "subject_id",
    "recordid": "record_id",
    "intakedate": "date",
    "datatype": "data_type",
    "subject": "subject_id",
    "subjectid": "subject_id",
    "intake_date": "date",
    "record": "record_id",
    "id": "record_id",
    "filename": "source_filename",
    "file": "source_filename",
    "file_name": "source_filename",
    "type": "data_type",
}
FIELDS = set(Metadata.model_fields) | {"source_filename"}


def preserve_original(workspace, data, suffix, folder="originals"):
    if suffix not in {".csv", ".xlsx", ".json", ".pdf", ".txt", ".png", ".jpg", ".jpeg"} or folder not in {
        "originals",
        "imported-metadata",
    }:
        raise InputError("Unsupported original source format.")
    checksum = hashlib.sha256(data).hexdigest()
    directory = workspace.path / "sources" / folder
    directory.mkdir(exist_ok=True)
    target = directory / (checksum + suffix)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
            raise InputError("An archived original failed its integrity check. Restore a verified backup.")
    else:
        fd, name = tempfile.mkstemp(dir=directory)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o400)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return {"sha256": checksum, "path": str(target.relative_to(workspace.path))}


def read_desktop_metadata(path):
    path = Path(path)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise InputError("Metadata must be at most 4 MB.")
    suffix = path.suffix.lower()
    if suffix == ".json":
        from .offline_archive import unique_object

        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
        raw = data if isinstance(data, list) else [data]
    else:
        if suffix == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as handle:
                values = list(csv.reader(handle))
        elif suffix == ".xlsx":
            from openpyxl import load_workbook

            book = load_workbook(path, read_only=True, data_only=True)
            try:
                values = list(book.active.values)
            finally:
                book.close()
        else:
            raise InputError("Choose CSV, XLSX or JSON metadata.")
        if not values:
            raise InputError("Metadata is empty. Include participant and date column names.")
        headers = [str(v).strip() if v is not None else "" for v in values[0]]
        if any(not v for v in headers) or len(headers) != len(set(headers)):
            raise InputError("Use nonempty, unique metadata column names.")
        raw = []
        for number, values_row in enumerate(values[1:], 2):
            if not any(v is not None and str(v).strip() for v in values_row):
                continue
            if len(values_row) != len(headers):
                raise InputError(f"Metadata row {number} has a different number of columns.")
            raw.append(dict(zip(headers, values_row, strict=True)))
    rows, unknown = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise InputError("Each metadata row must contain named fields.")
        normalized = {}
        for label, value in row.items():
            label = re.sub(r"[\s-]+", "_", str(label).strip().lower())
            key = ALIASES.get(label, label)
            if key not in FIELDS:
                unknown.add(label)
                continue
            if key in normalized:
                raise InputError(f"Multiple columns refer to {key}. Keep one of them.")
            if isinstance(value, datetime):
                value = value.date().isoformat()
            elif isinstance(value, date):
                value = value.isoformat()
            normalized[key] = str(value).strip() if value is not None else None
            normalized[key] = normalized[key] or None
        # Validate the values independently of the later explicit file association.
        metadata = Metadata.model_validate(
            {
                "record_id": "unassigned",
                **{k: v for k, v in normalized.items() if k in Metadata.model_fields and k != "record_id"},
            }
        ).model_dump(mode="json")
        metadata["record_id"] = normalized.get("record_id")
        if metadata["record_id"]:
            Metadata(record_id=metadata["record_id"])
        if normalized.get("source_filename"):
            metadata["source_filename"] = normalized["source_filename"]
        rows.append(metadata)
    if not rows:
        raise InputError("Metadata contains no data rows.")
    return rows, sorted(unknown)


@contextmanager
def sequence_state(workspace):
    with (workspace.path / "desktop-sequence.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = workspace.path / "desktop-sequence.json"
        try:
            state = json.loads(path.read_text()) if path.exists() else {"version": 1, "last": 0}
            if (
                not isinstance(state, dict)
                or state.get("version") != 1
                or type(state.get("last")) is not int
                or state["last"] < 0
                or not isinstance(state.get("attachments", {}), dict)
                or any(type(v) is not int or v < 0 for v in state.get("attachments", {}).values())
            ):
                raise ValueError("Invalid sequence state")
        except (ValueError, OSError) as exc:
            raise InputError(
                "The Study's record numbering file cannot be read. Restore a verified backup before importing; existing records have not been changed."
            ) from exc
        yield state
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.chmod(0o600)
        os.replace(temporary, path)


def reserve_id(workspace, parent=None, occupied=()):
    with workspace.factory() as session:
        records = list(session.scalars(select(Record).where(Record.project_id == workspace.project_id)))
        existing = {r.record_id for r in records}
        source_ids = set(existing)
        for record in records:
            source_ids.update(current_snapshot(session, record)["documents"])
    source_ids.update(occupied)
    with sequence_state(workspace) as state:
        if parent:
            Metadata(record_id=parent)
            if re.search(r"-A[1-9]\d*$", parent):
                raise InputError("Choose a main record for the attachment.")
            sequence = state.setdefault("attachments", {})
            maximum = (
                max(
                    [
                        sequence.get(parent, 0),
                        *[
                            int(s.rsplit("-A", 1)[1])
                            for s in source_ids
                            if re.fullmatch(re.escape(parent) + r"-A[1-9]\d*", s)
                        ],
                    ]
                )
                + 1
            )
            sequence[parent] = maximum
            return parent + f"-A{maximum}"
        maximum = (
            max([state.get("last", 0), *[int(s[1:]) for s in source_ids if re.fullmatch(r"R\d+", s)]]) + 1
        )
        state["last"] = maximum
        return f"R{maximum:04d}"


def stage_proposals(workspace, directory, descriptors, metadata_path=None, metadata_name=None):
    """Reserve proposed IDs without creating source revisions or guessing row order."""
    directory = Path(directory)
    rows, ignored_columns = read_desktop_metadata(metadata_path) if metadata_path else ([], [])
    existing, by_hash = {}, {}
    with workspace.factory() as session:
        for record in session.scalars(select(Record).where(Record.project_id == workspace.project_id)):
            snapshot = current_snapshot(session, record)
            existing[record.record_id] = snapshot["metadata"].get(record.record_id, {})
            for source_id, doc in snapshot["documents"].items():
                by_hash.setdefault(doc["sha256"], []).append(source_id)
    explicit = {r["record_id"] for r in rows if r.get("record_id")}
    initial = []
    for entry in descriptors:
        filename = entry["filename"]
        stem = Path(filename).stem
        chosen = stem if re.fullmatch(r"R\d+(?:-A[1-9]\d*)?", stem) or stem in explicit else None
        named = [
            r
            for r in rows
            if r.get("source_filename")
            and r.get("record_id")
            and (
                r["source_filename"] == filename
                or Path(r["source_filename"]).name == Path(filename).name
                and sum(Path(e["filename"]).name == Path(filename).name for e in descriptors) == 1
            )
        ]
        if len(named) == 1:
            chosen = named[0]["record_id"]
        if len(descriptors) == len(rows) == 1 and rows[0].get("record_id") and not chosen:
            chosen = rows[0]["record_id"]
        checksum = hashlib.sha256((directory / entry["file"]).read_bytes()).hexdigest()
        matches = by_hash.get(checksum, [])
        if not chosen and len(matches) == 1:
            chosen = matches[0]
        initial.append({**entry, "source_id": chosen, "sha256": checksum, "matches": matches})
    with sequence_state(workspace) as state:
        maximum = max(
            [
                state.get("last", 0),
                *[int(r[1:]) for r in existing if re.fullmatch(r"R\d+", r)],
                *[
                    int(e["source_id"][1:])
                    for e in initial
                    if e["source_id"] and re.fullmatch(r"R\d+", e["source_id"])
                ],
            ]
        )
        occupied = set(existing) | explicit | {e["source_id"] for e in initial if e["source_id"]}
        for entry in initial:
            if not entry["source_id"]:
                while True:
                    maximum += 1
                    proposed = f"R{maximum:04d}"
                    if proposed not in occupied:
                        break
                entry["source_id"] = proposed
                entry["generated"] = True
                occupied.add(proposed)
        state["last"] = maximum
    from .ingest import split_id

    roots = sorted({split_id(e["source_id"])[0] for e in initial})
    if not metadata_path:
        rows = [{**Metadata(record_id=rid).model_dump(mode="json"), **existing.get(rid, {})} for rid in roots]
    elif len(roots) == len(rows) == 1 and not rows[0].get("record_id"):
        rows[0]["record_id"] = roots[0]
    # File-name associations without a preassigned ID can also be unambiguous.
    for row in rows:
        if row.get("record_id") or not row.get("source_filename"):
            continue
        matches = [
            e
            for e in initial
            if row["source_filename"] == e["filename"]
            or Path(row["source_filename"]).name == Path(e["filename"]).name
        ]
        if len(matches) == 1:
            row["record_id"] = split_id(matches[0]["source_id"])[0]
    result = {
        "token": directory.name,
        "entries": initial,
        "metadata": rows,
        "ignored_columns": ignored_columns,
    }
    manifest = {
        "files": [e["file"] for e in initial],
        "original_names": {e["file"]: e["filename"] for e in initial},
        "metadata_file": metadata_path.name if metadata_path else None,
        "metadata_original_name": metadata_name or (metadata_path.name if metadata_path else None),
        "proposal": result,
    }
    (directory / "upload.json").write_text(json.dumps(manifest, ensure_ascii=False))
    return result
