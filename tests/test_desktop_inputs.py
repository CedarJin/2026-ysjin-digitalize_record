# ruff: noqa: F811
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from openpyxl import Workbook
from sqlalchemy import select
from test_ingest import pdf, ws  # noqa: F401

from food_record_digitizer.backup import create_backup, restore_backup
from food_record_digitizer.db import Batch, Record
from food_record_digitizer.desktop_import import commit_plan, preview_plan
from food_record_digitizer.desktop_inputs import read_desktop_metadata, reserve_id, stage_proposals
from food_record_digitizer.ingest import InputError, current_snapshot


def staged(ws, tmp_path, name, metadata=None):
    directory = tmp_path / name
    directory.mkdir()
    pdf(directory / "file-0.pdf")
    meta = None
    if metadata:
        meta = directory / "metadata.csv"
        meta.write_text(metadata)
    return directory, stage_proposals(
        ws, directory, [{"file": "file-0.pdf", "filename": "Unsorted scan.pdf"}], meta
    )


def test_minimal_metadata_aliases_and_duplicate_columns(tmp_path):
    path = tmp_path / "metadata.csv"
    path.write_text("participant,date\nP1,\n")
    rows, ignored = read_desktop_metadata(path)
    assert rows[0]["subject_id"] == "P1" and rows[0]["date"] is None and rows[0]["record_id"] is None
    assert ignored == []
    path.write_text("participant,subject_id,date\nP1,P2,\n")
    with pytest.raises(InputError, match="Multiple columns"):
        read_desktop_metadata(path)
    book = Workbook()
    book.active.append(["Participant ID", "Intake date", "Timepoint"])
    book.active.append(["P2", "2026-10-21", "T1"])
    book.save(tmp_path / "metadata.xlsx")
    rows, _ = read_desktop_metadata(tmp_path / "metadata.xlsx")
    assert rows[0]["subject_id"] == "P2" and rows[0]["timepoint"] == "T1"
    path = tmp_path / "metadata.json"
    path.write_text('[{"participant":"P3","date":null,"extra":"keep original"}]')
    rows, ignored = read_desktop_metadata(path)
    assert rows[0]["subject_id"] == "P3" and ignored == ["extra"]


@pytest.mark.parametrize(
    "contents",
    [
        "{",
        "[]",
        '{"version":1,"last":-1}',
        '{"version":1,"last":true}',
        '{"version":1,"last":2,"attachments":{"R0001":"bad"}}',
    ],
)
def test_corrupt_numbering_never_resets_or_overwrites(ws, contents):
    path = ws.path / "desktop-sequence.json"
    path.write_text(contents)
    with pytest.raises(InputError, match="numbering file"):
        reserve_id(ws)
    assert path.read_text() == contents


def test_automatic_id_provenance_reimport_backup_and_resume(ws, tmp_path):
    directory, proposal = staged(ws, tmp_path, "first", "participant,date,extra\nP1,,original value\n")
    assert proposal["entries"][0]["source_id"] == "R0001"
    assert proposal["metadata"][0]["record_id"] == "R0001"
    assert proposal["ignored_columns"] == ["extra"]
    preview_plan(ws, directory, proposal["entries"], proposal["metadata"])
    with ws.factory() as s:
        assert list(s.scalars(select(Record))) == []
    first = commit_plan(ws, directory, proposal["entries"], proposal["metadata"])
    assert first["changed"] == ["R0001"]
    with ws.factory() as s:
        record = s.scalar(select(Record))
        snapshot = current_snapshot(s, record)
        assert snapshot["documents"]["R0001"]["original_filename"] == "Unsorted scan.pdf"
        assert (ws.path / "sources" / snapshot["documents"]["R0001"]["managed_file"]).name == "R0001.pdf"
        assert snapshot["metadata"]["R0001"]["date"] is None
        batch = s.get(Batch, first["batch_id"])
        original = batch.manifest["import_context"]["original_metadata"]
        assert (ws.path / original["path"]).read_text().endswith("original value\n")
    assert commit_plan(ws, directory, proposal["entries"], proposal["metadata"])["changed"] == []
    _, repeated = staged(ws, tmp_path, "again")
    assert repeated["entries"][0]["source_id"] == "R0001"
    assert repeated["entries"][0]["matches"] == ["R0001"]
    assert repeated["metadata"][0]["subject_id"] == "P1"
    assert reserve_id(ws) == "R0002"
    assert reserve_id(ws, "R0001") == "R0001-A1"
    assert reserve_id(ws, "R0001") == "R0001-A2"
    backup = tmp_path / "study.zip"
    create_backup(ws, backup)
    destination = tmp_path / "restored"
    restore_backup(backup, destination)
    from food_record_digitizer.workspace import Workspace

    restored = Workspace(destination)
    assert reserve_id(restored) == "R0003"
    assert reserve_id(restored, "R0001") == "R0001-A3"


def test_two_column_batch_requires_explicit_association(ws, tmp_path):
    directory = tmp_path / "batch"
    directory.mkdir()
    pdf(directory / "file-0.pdf")
    pdf(directory / "file-1.pdf", pages=2)
    metadata = directory / "metadata.csv"
    metadata.write_text("participant,date\nP1,2026-01-01\nP2,\n")
    p = stage_proposals(
        ws, directory, [{"file": f"file-{i}.pdf", "filename": f"scan {i}.pdf"} for i in range(2)], metadata
    )
    assert [e["source_id"] for e in p["entries"]] == ["R0001", "R0002"]
    assert all(m["record_id"] is None for m in p["metadata"])
    with pytest.raises(InputError, match="Choose a scan"):
        preview_plan(ws, directory, p["entries"], p["metadata"])
    p["metadata"][0]["record_id"] = "R0002"
    p["metadata"][1]["record_id"] = "R0001"
    commit_plan(ws, directory, p["entries"], p["metadata"])
    with ws.factory() as s:
        rows = {
            r.record_id: current_snapshot(s, r)["metadata"][r.record_id]["subject_id"]
            for r in s.scalars(select(Record))
        }
        assert rows == {"R0001": "P2", "R0002": "P1"}


def test_concurrent_id_reservations_never_collide(ws):
    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(lambda _: reserve_id(ws), range(12)))
    assert len(set(ids)) == 12 and set(ids) == {f"R{i:04d}" for i in range(1, 13)}


def test_metadata_edit_keeps_transcription_invalidates_review_without_api(ws, tmp_path):
    from fastapi.testclient import TestClient
    from test_day_review import add_source, finish

    from food_record_digitizer import db
    from food_record_digitizer.desktop_server import study_app
    from food_record_digitizer.review import DEFAULT_POLICY

    add_source(ws, tmp_path)
    app = study_app(ws, "secret")
    try:
        client = TestClient(app, headers={"x-desktop-capability": "secret"})
        owner = client.get("/api/me").json()["id"]
        service = app.state.review
        service.set_policy(owner, {**DEFAULT_POLICY, "sample_percent": 0, "high_risk_flags": False})
        day = service.queue(owner)[0]["record_id"]
        task = service.assign(owner, day, owner)
        finish(service, owner, task)
        service.approve(owner, day)
        release = service.release(owner, [day])
        with ws.factory() as s:
            frozen = json.dumps(s.get(db.ReviewRelease, release).payload, sort_keys=True)
            original_candidate = s.scalar(select(db.Candidate))
            candidate_id, content = original_candidate.id, original_candidate.content
        original = client.get("/desktop/metadata/R001").json()
        proposal = client.post(
            "/desktop/metadata/R001/preview",
            json={"version": original["version"], "metadata": {"subject_id": "P2", "date": None}},
        )
        assert proposal.status_code == 200, proposal.text
        checked = proposal.json()
        assert checked["affected_reviews"][0]["records"] == ["R001"]
        assert client.get("/desktop/metadata/R001").json() == original
        saved = client.post(
            "/desktop/metadata/R001/confirm", json={"version": checked["version"], "token": checked["token"]}
        )
        assert saved.status_code == 200, saved.text
        assert (
            client.post(
                "/desktop/metadata/R001/confirm",
                json={"version": checked["version"], "token": checked["token"]},
            ).status_code
            == 400
        )
        rows = service.queue(owner)
        assert (
            len(rows) == 1
            and rows[0]["subject_id"] == "P2"
            and rows[0]["date"] == "2026-01-01"
            and rows[0]["state"] != "approved"
        )
        with ws.factory() as s:
            current = s.scalar(select(db.Candidate).where(db.Candidate.input_version == 2))
            assert current.provenance["source_candidate_id"] == candidate_id
            assert current.content["items"] == content["items"]
            assert json.dumps(s.get(db.ReviewRelease, release).payload, sort_keys=True) == frozen
            assert s.get(db.DayReviewTask, task).status == "stale"
            assert list(s.scalars(select(db.ExtractionRun))) == []
        assert app.state.queue.preview(["R001"])["requests"] == 0
        assert app.state.queue.preview(["R001"])["reuse"] == 1
    finally:
        app.state.queue.close()


def test_corrupt_pending_import_keeps_study_open_and_original_pointer(ws):
    from fastapi.testclient import TestClient

    from food_record_digitizer.desktop_server import study_app

    pointer = ws.path / "desktop-current-import.json"
    pointer.write_text("{broken")
    app = study_app(ws, "secret")
    try:
        client = TestClient(app, headers={"x-desktop-capability": "secret"})
        response = client.get("/desktop/state")
        assert response.status_code == 200
        assert response.json()["pending_import"] is None
        assert "Existing Study records are safe" in response.json()["pending_import_error"]
        assert pointer.read_text() == "{broken"
    finally:
        app.state.queue.close()


def test_failed_import_copy_rolls_back_and_same_preview_can_retry(ws, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from food_record_digitizer.desktop_server import study_app

    directory = ws.path / "desktop-imports" / ("f" * 40)
    directory.mkdir(parents=True)
    pdf(directory / "file-0.pdf")
    proposal = stage_proposals(ws, directory, [{"file": "file-0.pdf", "filename": "Unsorted.pdf"}])
    proposal["metadata"][0]["subject_id"] = "P1"
    app = study_app(ws, "secret")
    try:
        client = TestClient(app, headers={"x-desktop-capability": "secret"})
        checked = client.post("/desktop/import/preview", json=proposal)
        assert checked.status_code == 200, checked.text

        def fail_copy(*args):
            raise OSError("Synthetic full disk")

        with monkeypatch.context() as patch:
            patch.setattr(ws.store, "named_copy", fail_copy)
            failed = client.post("/desktop/import/confirm", json=proposal)
            assert failed.status_code == 503
            assert "free space" in failed.json()["error"]
        with ws.factory() as session:
            assert list(session.scalars(select(Record))) == []
        retried = client.post("/desktop/import/confirm", json=proposal)
        assert retried.status_code == 200, retried.text
        assert retried.json()["changed"] == ["R0001"]
        with ws.factory() as session:
            records = list(session.scalars(select(Record)))
            assert len(records) == 1 and records[0].input_version == 1
    finally:
        app.state.queue.close()
