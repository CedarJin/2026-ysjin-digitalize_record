from pathlib import Path

import pytest
from pypdf import PdfWriter
from sqlalchemy import select

from food_record_digitizer.db import InputRevision, Record
from food_record_digitizer.ingest import (
    InputError,
    current_snapshot,
    ingest_batch,
    ingest_one,
    read_metadata,
    split_id,
)
from food_record_digitizer.schemas import Metadata
from food_record_digitizer.workspace import Workspace


def pdf(path: Path, pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    writer.write(path)
    return path


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.delenv("FOOD_RECORD_DATABASE_URL", raising=False)
    return Workspace.initialize(tmp_path / "work", "study", "Test study")


def test_batch_and_single_are_idempotent_and_attachment_is_not_record(ws, tmp_path):
    main, attachment = pdf(tmp_path / "R001.pdf"), pdf(tmp_path / "R001-A1.pdf")
    meta = Metadata(record_id="R001", subject_id="001")
    first = ingest_one(ws.factory, ws.store, ws.project_id, main, meta, [attachment])
    second = ingest_batch(ws.factory, ws.store, ws.project_id, [attachment, main], [meta])
    assert first["changed"] == ["R001"]
    assert second["changed"] == []
    assert second["unchanged"] == ["R001"]
    assert len(ws.records()) == 1
    with ws.factory() as session:
        record = session.scalar(select(Record))
        snapshot = current_snapshot(session, record)
        assert record.input_version == 1
        assert set(snapshot["documents"]) == {"R001", "R001-A1"}
        assert snapshot["documents"]["R001-A1"]["role"] == "attachment"


def test_new_attachment_and_metadata_revisions_preserve_old_sources(ws, tmp_path):
    main = pdf(tmp_path / "R001.pdf")
    ingest_one(ws.factory, ws.store, ws.project_id, main, Metadata(record_id="R001", subject_id="S1"))
    attachment = pdf(tmp_path / "R001-A1.pdf", 2)
    ingest_batch(ws.factory, ws.store, ws.project_id, [attachment], [])
    ingest_batch(ws.factory, ws.store, ws.project_id, [], [Metadata(record_id="R001", subject_id="S2")])
    with ws.factory() as session:
        revisions = list(session.scalars(select(InputRevision).order_by(InputRevision.version)))
        assert len(revisions) == 3
        assert len(revisions[0].snapshot["documents"]) == 1
        assert revisions[0].snapshot["metadata"]["R001"]["subject_id"] == "S1"
        assert revisions[2].snapshot["metadata"]["R001"]["subject_id"] == "S2"
        assert len(revisions[2].snapshot["documents"]) == 2
        for revision in revisions:
            for doc in revision.snapshot["documents"].values():
                assert ws.store.path(doc["sha256"]).exists()


def test_missing_orphan_excluded_corrupt_are_retained(ws, tmp_path):
    attachment = pdf(tmp_path / "R001-A1.pdf")
    excluded = pdf(tmp_path / "R002.pdf")
    corrupt = tmp_path / "R003.pdf"
    corrupt.write_bytes(b"invalid")
    ingest_batch(
        ws.factory,
        ws.store,
        ws.project_id,
        [attachment, excluded, corrupt],
        [
            Metadata(record_id="R001", subject_id="S1"),
            Metadata(record_id="R002", subject_id="S2", flag="don't use"),
            Metadata(record_id="R003", subject_id="S3"),
            Metadata(record_id="R004", subject_id="S4"),
        ],
    )
    assert {r.record_id: r.state for r in ws.records()} == {
        "R001": "missing_source",
        "R002": "exclusion_pending",
        "R003": "blocked",
        "R004": "missing_source",
    }
    main = pdf(tmp_path / "R001.pdf")
    ingest_batch(ws.factory, ws.store, ws.project_id, [main], [])
    assert ws.records()[0].state == "pending_extraction"


def test_duplicate_ids_rejected_before_writing(ws, tmp_path):
    main = pdf(tmp_path / "R001.pdf")
    with pytest.raises(InputError):
        ingest_batch(ws.factory, ws.store, ws.project_id, [main, main], [])
    with pytest.raises(InputError):
        ingest_batch(
            ws.factory, ws.store, ws.project_id, [], [Metadata(record_id="R001"), Metadata(record_id="R001")]
        )
    assert ws.records() == []


def test_single_cannot_mix_unrelated_attachment(ws, tmp_path):
    with pytest.raises(InputError):
        ingest_one(
            ws.factory,
            ws.store,
            ws.project_id,
            pdf(tmp_path / "R001.pdf"),
            Metadata(record_id="R001"),
            [pdf(tmp_path / "R002-A1.pdf")],
        )


def test_metadata_text_ids_and_missing_values(tmp_path):
    path = tmp_path / "meta.csv"
    path.write_text("record_id,subject_id,data_type,date,timepoint,flag\nR001,0001,hab,2025-01-02,pre,\n")
    row = read_metadata(path)[0]
    assert row.subject_id == "0001"
    assert row.flag is None
    assert str(row.date) == "2025-01-02"
    assert split_id("R001-A12") == ("R001", 12)
    with pytest.raises(InputError):
        split_id("R001-A0")


def test_corrupt_cached_source_cannot_be_reused(ws, tmp_path):
    main = pdf(tmp_path / "R001.pdf")
    info = ws.store.put(main)
    ws.store.path(info["sha256"]).write_bytes(b"changed-after-import")
    with pytest.raises(InputError, match="integrity"):
        ws.store.put(main)


def test_identical_content_under_different_ids_is_reported(ws, tmp_path):
    first = pdf(tmp_path / "R001.pdf")
    second = tmp_path / "R002.pdf"
    second.write_bytes(first.read_bytes())
    report = ingest_batch(
        ws.factory,
        ws.store,
        ws.project_id,
        [first, second],
        [Metadata(record_id="R001", subject_id="S1"), Metadata(record_id="R002", subject_id="S2")],
    )
    assert report["duplicate_content"] == [["R001", "R002"]]
    assert len(ws.records()) == 2


@pytest.mark.parametrize(
    "contents",
    [
        "record_id,subject_id,subject_id\nR001,S1,S2\n",
        "record_id,subject_id\nR001,S1,unexpected-value\n",
        "record_id,subject_id\nR001\n",
        "record_id, subject_id ,subject_id\nR001,S1,S2\n",
        "record_id,\nR001,unlabeled-value\n",
    ],
)
def test_ambiguous_metadata_tables_are_rejected(tmp_path, contents):
    path = tmp_path / "metadata.csv"
    path.write_text(contents)
    with pytest.raises(InputError):
        read_metadata(path)


def test_duplicates_arriving_in_separate_batches_are_reported(ws, tmp_path):
    from food_record_digitizer.cli import report_data

    first = pdf(tmp_path / "R001.pdf")
    second = tmp_path / "R002.pdf"
    second.write_bytes(first.read_bytes())
    ingest_one(ws.factory, ws.store, ws.project_id, first, Metadata(record_id="R001", subject_id="S1"))
    imported = ingest_one(
        ws.factory, ws.store, ws.project_id, second, Metadata(record_id="R002", subject_id="S2")
    )
    assert imported["duplicate_content"] == [["R001", "R002"]]
    assert report_data(ws)["duplicate_content"] == [["R001", "R002"]]
    # Replacing the second file removes the current duplicate but preserves old revisions.
    pdf(second, pages=2)
    ingest_one(ws.factory, ws.store, ws.project_id, second, Metadata(record_id="R002", subject_id="S2"))
    assert report_data(ws)["duplicate_content"] == []


def test_duplicate_json_keys_are_not_silently_overwritten(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text('{"record_id":"R001","subject_id":"S1","subject_id":"S2"}')
    with pytest.raises(InputError, match="Duplicate"):
        read_metadata(path)
