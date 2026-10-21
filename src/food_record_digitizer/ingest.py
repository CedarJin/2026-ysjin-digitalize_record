"""Incremental source registration; no model calls or destructive updates."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import select

from .db import Audit, Batch, Candidate, InputRevision, Project, Record
from .schemas import Metadata


class InputError(ValueError):
    pass


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def split_id(source_id: str) -> tuple[str, int | None]:
    Metadata(record_id=source_id)
    match = re.fullmatch(r"(.+)-A([1-9][0-9]*)", source_id)
    if match:
        return match[1], int(match[2])
    if re.search(r"-A\d+$", source_id):
        raise InputError(f"Invalid attachment suffix: {source_id}")
    return source_id, None


def _metadata_columns(headers) -> list[str]:
    columns = [str(key).strip() if key is not None else "" for key in headers]
    if not columns or any(not key for key in columns):
        raise InputError("Metadata columns must have nonempty names")
    if len(set(columns)) != len(columns):
        raise InputError("Duplicate metadata column names are not allowed")
    if set(columns) - Metadata.model_fields.keys():
        raise InputError("Unexpected metadata column; no columns were discarded")
    if "record_id" not in columns:
        raise InputError("Metadata must include the record_id column")
    return columns


def _table_rows(values) -> list[dict]:
    values = iter(values)
    headers = next(values, None)
    if headers is None:
        raise InputError("Metadata table is empty; include a header row")
    columns = _metadata_columns(headers)
    rows = []
    for index, row in enumerate(values, start=2):
        if not any(value is not None and str(value).strip() for value in row):
            continue
        if len(row) != len(columns):
            raise InputError(f"Metadata row {index} does not match the number of columns")
        rows.append(dict(zip(columns, row, strict=True)))
    return rows


def _json_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("Duplicate JSON field names are not allowed")
        result[key] = value
    return result


def read_metadata(path: Path) -> list[Metadata]:
    if path.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook

        book = load_workbook(path, read_only=True, data_only=True)
        try:
            rows = _table_rows(book.active.values)
        finally:
            book.close()
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = _table_rows(csv.reader(handle))
    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text(), object_pairs_hook=_json_object)
        rows = data if isinstance(data, list) else [data]
    else:
        raise InputError("Metadata must be XLSX, CSV or JSON")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise InputError("Each metadata row must be an object")
        columns = _metadata_columns(row.keys())
        normalized = {}
        for key, value in zip(columns, row.values(), strict=True):
            if isinstance(value, datetime):
                value = value.date().isoformat()
            elif isinstance(value, date):
                value = value.isoformat()
            if isinstance(value, str):
                value = value.strip() or None
            normalized[key] = value
        item = Metadata.model_validate(normalized)
        if item.record_id in seen:
            raise InputError(f"Duplicate metadata ID: {item.record_id}")
        seen.add(item.record_id)
        result.append(item)
    return result


class BlobStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, path: Path) -> dict:
        # Copy first, validate the exact stored bytes, then atomically register them.
        payload = path.read_bytes()
        sha = hashlib.sha256(payload).hexdigest()
        target = self.root / sha[:2] / f"{sha}.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != sha:
            raise InputError("Stored source failed its integrity check; restore it from a verified backup")
        if not target.exists():
            fd, temp = tempfile.mkstemp(dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, target)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        try:
            reader = PdfReader(target, strict=False)
            if reader.is_encrypted:
                raise InputError("Encrypted PDF")
            count = len(reader.pages)
            if count == 0:
                raise InputError("PDF has no pages")
            error = None
        except Exception:
            count, error = 0, "unreadable_pdf"
        return {"sha256": sha, "page_count": count, "error": error}

    def named_copy(self, source_id: str, sha: str, version: int) -> str:
        Metadata(record_id=source_id)
        directory = self.root / "records" / source_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (source_id + ("" if version == 1 else f"-v{version}") + ".pdf")
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                raise InputError(
                    "A managed source filename conflicts with different content. Restore a verified backup."
                )
        else:
            data = self.verified_path(sha).read_bytes()
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
        return str(target.relative_to(self.root))

    def path(self, sha: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise InputError("Invalid blob hash")
        return self.root / sha[:2] / f"{sha}.pdf"

    def verified_path(self, sha: str) -> Path:
        """Check archived bytes again before using them as extraction evidence."""
        path = self.path(sha)
        checksum = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    checksum.update(chunk)
        except OSError as exc:
            raise InputError("Stored source is missing or unreadable; restore a verified backup") from exc
        if checksum.hexdigest() != sha:
            raise InputError("Stored source failed its integrity check; restore a verified backup")
        return path


def current_snapshot(session, record: Record) -> dict:
    if not record.input_version:
        return {"metadata": {}, "documents": {}}
    revision = session.scalar(
        select(InputRevision).where(
            InputRevision.record_pk == record.id, InputRevision.version == record.input_version
        )
    )
    return json.loads(canonical(revision.snapshot))


def current_duplicate_content(session, project_id: str) -> list[list[str]]:
    """Detect copies across current records, including separate import batches."""
    snapshots = session.scalars(
        select(InputRevision.snapshot)
        .join(
            Record, (Record.id == InputRevision.record_pk) & (Record.input_version == InputRevision.version)
        )
        .where(Record.project_id == project_id)
    )
    hashes = defaultdict(list)
    for snapshot in snapshots:
        for source_id, document in snapshot["documents"].items():
            hashes[document["sha256"]].append(source_id)
    return sorted(sorted(ids) for ids in hashes.values() if len(ids) > 1)


def inspect_inputs(paths: list[Path], rows: list[Metadata]) -> dict:
    """Read-only association report, used before modifying a workspace."""
    stems = [p.stem for p in paths]
    if len(stems) != len(set(stems)):
        raise InputError("Duplicate PDF filenames; provide an unambiguous batch")
    ids = [r.record_id for r in rows]
    if len(ids) != len(set(ids)):
        raise InputError("Duplicate metadata IDs")
    all_ids = set(stems) | set(ids)
    groups = defaultdict(list)
    for source_id in sorted(all_ids):
        root, index = split_id(source_id)
        groups[root].append(
            {"source_record_id": source_id, "role": "main" if index is None else "attachment"}
        )
    return {
        "pdf_count": len(paths),
        "metadata_rows": len(rows),
        "record_count": len(groups),
        "attachment_count": sum(split_id(s)[1] is not None for s in stems),
        "missing_source": sorted(set(ids) - set(stems)),
        "missing_metadata": sorted(set(stems) - set(ids)),
        "groups": dict(groups),
    }


def ingest_batch(
    factory,
    store: BlobStore,
    project_id: str,
    paths: list[Path],
    rows: list[Metadata],
    actor: str = "local-operator",
    source_names: dict[str, str] | None = None,
    import_context: dict | None = None,
    reuse_metadata_candidates: bool = False,
    source_details: dict[str, dict] | None = None,
) -> dict:
    report = inspect_inputs(paths, rows)
    # Files and metadata can arrive separately; merge only supplied document/row IDs.
    incoming = {p.stem: store.put(p) for p in paths}
    metadata = {r.record_id: r.model_dump(mode="json") for r in rows}
    changed, unchanged = [], []
    with factory.begin() as session:
        if session.get(Project, project_id) is None:
            raise InputError("Unknown project; initialize it first")
        for record_id, sources in report["groups"].items():
            record = session.scalar(
                select(Record)
                .where(Record.project_id == project_id, Record.record_id == record_id)
                .with_for_update()
            )
            if record is None:
                record = Record(project_id=project_id, record_id=record_id, input_version=0)
                session.add(record)
                session.flush()
            snapshot = current_snapshot(session, record)
            previous_documents = dict(snapshot["documents"])
            previous_version = record.input_version
            for source in sources:
                source_id = source["source_record_id"]
                if source_id in incoming:
                    previous_document = snapshot["documents"].get(source_id, {})
                    document = {**incoming[source_id], **source}
                    if source_details and source_id in source_details:
                        document["attachment_original"] = source_details[source_id]
                    if previous_document.get("sha256") == document["sha256"]:
                        for key in ("original_filename", "managed_file", "attachment_original"):
                            if previous_document.get(key):
                                document[key] = previous_document[key]
                    elif source_names and source_id in source_names:
                        document["original_filename"] = source_names[source_id]
                        document["managed_file"] = store.named_copy(
                            source_id, document["sha256"], record.input_version + 1
                        )
                    snapshot["documents"][source_id] = document
                if source_id in metadata:
                    snapshot["metadata"][source_id] = metadata[source_id]
            fingerprint = digest(snapshot)
            if fingerprint == record.fingerprint:
                unchanged.append(record_id)
                continue
            old_version = record.input_version
            record.input_version += 1
            record.fingerprint = fingerprint
            record.approved_id = None
            from .db import ReviewTask

            for task in session.scalars(select(ReviewTask).where(ReviewTask.record_pk == record.id)):
                if task.status not in {"cancelled", "returned", "stale"}:
                    task.status = "stale"
            documents = snapshot["documents"]
            main = documents.get(record_id)
            meta = snapshot["metadata"].get(record_id)
            if not main:
                record.state = "missing_source"
            elif any(d["error"] for d in documents.values()) or not meta or not meta.get("subject_id"):
                record.state = "blocked"
            elif any("don't use" in (m.get("flag") or "").lower() for m in snapshot["metadata"].values()):
                record.state = "exclusion_pending"
            else:
                record.state = "pending_extraction"
            if (
                reuse_metadata_candidates
                and previous_documents == snapshot["documents"]
                and record.state == "pending_extraction"
            ):
                previous_candidate = None
                for candidate in session.scalars(
                    select(Candidate)
                    .where(Candidate.record_pk == record.id)
                    .order_by(Candidate.created_at.desc(), Candidate.id.desc())
                ):
                    revision = session.scalar(
                        select(InputRevision).where(
                            InputRevision.record_pk == record.id,
                            InputRevision.version == candidate.input_version,
                        )
                    )
                    if revision and revision.snapshot["documents"] == snapshot["documents"]:
                        previous_candidate = candidate
                        break
                if previous_candidate:
                    content = json.loads(canonical(previous_candidate.content))
                    for page in content.get("pages", []):
                        page["reviewed"] = False
                    session.add(
                        Candidate(
                            record_pk=record.id,
                            input_version=record.input_version,
                            content=content,
                            provenance={
                                **previous_candidate.provenance,
                                "source_candidate_id": previous_candidate.id,
                                "ai_input_version": previous_candidate.provenance.get(
                                    "ai_input_version", previous_version
                                ),
                                "metadata_update": {"from": previous_version, "to": record.input_version},
                            },
                        )
                    )
                    record.state = "pending_review"
            session.add(
                InputRevision(
                    record_pk=record.id,
                    version=record.input_version,
                    fingerprint=fingerprint,
                    snapshot=snapshot,
                )
            )
            session.add(
                Audit(
                    project_id=project_id,
                    actor_id=actor,
                    action="input_revised",
                    target_id=record.id,
                    detail={"from": old_version, "to": record.input_version},
                )
            )
            changed.append(record_id)
        session.flush()
        report["duplicate_content"] = current_duplicate_content(session, project_id)
        batch = Batch(
            project_id=project_id,
            manifest={
                **report,
                "changed": changed,
                "unchanged": unchanged,
                "source_paths": {p.stem: str(p.resolve()) for p in paths},
                "metadata_sha256": digest(metadata),
                **({"import_context": import_context} if import_context else {}),
            },
        )
        session.add(batch)
        session.flush()
        return {"batch_id": batch.id, **batch.manifest}


def ingest_one(
    factory,
    store,
    project_id,
    pdf: Path,
    metadata: Metadata,
    attachments: list[Path] | None = None,
    actor="local-operator",
):
    if pdf.stem != metadata.record_id or split_id(pdf.stem)[1] is not None:
        raise InputError("Single-record metadata must match the main PDF ID")
    attachments = attachments or []
    if any(split_id(p.stem)[0] != metadata.record_id or split_id(p.stem)[1] is None for p in attachments):
        raise InputError("All attachments must belong to the main record")
    return ingest_batch(factory, store, project_id, [pdf, *attachments], [metadata], actor)
