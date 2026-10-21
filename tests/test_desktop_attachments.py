# ruff: noqa: F811
import io
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select
from test_ingest import pdf, ws  # noqa: F401

from food_record_digitizer.backup import create_backup, restore_backup
from food_record_digitizer.db import ExtractionRun, Record
from food_record_digitizer.desktop_attachments import stage_attachment
from food_record_digitizer.desktop_import import commit_plan, preview_plan
from food_record_digitizer.ingest import InputError, current_snapshot, ingest_one
from food_record_digitizer.schemas import Metadata
from food_record_digitizer.workspace import Workspace


def test_text_image_pdf_derivatives_preserve_raw_and_backup(ws, tmp_path):
    main = pdf(tmp_path / "R0001.pdf")
    ingest_one(ws.factory, ws.store, ws.project_id, main, Metadata(record_id="R0001", subject_id="P1"))
    image = io.BytesIO()
    Image.new("RGBA", (600, 800), (255, 0, 0, 120)).save(image, "PNG")
    inputs = [
        (b"Clarification: breakfast at 08:30.\n\xe7\xb1\xb3\xe9\xa5\xad", "note.txt", "researcher_note"),
        (image.getvalue(), "photo.png", "participant_material"),
        (main.read_bytes(), "extra.pdf", "participant_material"),
    ]
    for index, (raw, name, origin) in enumerate(inputs, 1):
        proposal = stage_attachment(ws, "R0001", raw, name, origin)
        assert proposal["entries"][0]["source_id"] == f"R0001-A{index}"
        directory = ws.path / "desktop-imports" / proposal["token"]
        assert len(PdfReader(directory / "file-0.pdf").pages) >= 1
        report = preview_plan(ws, directory, proposal["entries"], [])
        assert report["revised_records"] == ["R0001"]
        commit_plan(ws, directory, proposal["entries"], [])
        with ws.factory() as session:
            record = session.scalar(select(Record))
            snapshot = current_snapshot(session, record)
            assert len(list(session.scalars(select(Record)))) == 1
            doc = snapshot["documents"][f"R0001-A{index}"]
            original = doc["attachment_original"]
            assert original["origin"] == origin and original["filename"] == name
            assert (ws.path / original["path"]).read_bytes() == raw
            assert doc["role"] == "attachment"
            assert list(session.scalars(select(ExtractionRun))) == []
    backup = tmp_path / "backup.zip"
    create_backup(ws, backup)
    restore_backup(backup, tmp_path / "restored")
    restored = Workspace(tmp_path / "restored")
    with restored.factory() as session:
        snapshot = current_snapshot(session, session.scalar(select(Record)))
        for doc in snapshot["documents"].values():
            if doc.get("attachment_original"):
                assert (restored.path / doc["attachment_original"]["path"]).exists()


@pytest.mark.parametrize(
    "raw,suffix", [(b"bad", ".pdf"), (b"bad", ".png"), (b" ", ".txt"), (b"\xff", ".txt"), (b"bad", ".exe")]
)
def test_invalid_attachment_does_not_allocate_id(ws, raw, suffix):
    with pytest.raises(InputError):
        stage_attachment(ws, "R0001", raw, "bad" + suffix, "researcher_note")
    assert not (ws.path / "desktop-sequence.json").exists()


def test_attachment_requires_declared_origin(ws):
    with pytest.raises(InputError, match="participant material"):
        stage_attachment(ws, "R0001", b"text", "note.txt", "unknown")
    assert not (ws.path / "desktop-sequence.json").exists()


def test_added_attachment_manual_review_keeps_old_ai_food_without_claiming_new_ai(ws, tmp_path):
    from fastapi.testclient import TestClient
    from test_day_review import add_source

    from food_record_digitizer.db import Candidate
    from food_record_digitizer.desktop_server import study_app

    add_source(ws, tmp_path)
    app = study_app(ws, "test-secret")
    try:
        client = TestClient(app, headers={"x-desktop-capability": "test-secret"})
        with ws.factory() as session:
            original = session.scalar(select(Candidate))
            original_id, original_content = original.id, original.content
        staged = client.post(
            "/desktop/attachments/R001",
            data={
                "text": "Participant clarified: one cup, not two.",
                "origin": "participant_material",
            },
        )
        assert staged.status_code == 200, staged.text
        proposal = staged.json()
        preview = client.post("/desktop/import/preview", json=proposal)
        assert preview.status_code == 200, preview.text
        saved = client.post("/desktop/import/confirm", json={**proposal, "allow_update": True})
        assert saved.status_code == 200, saved.text
        with ws.factory() as session:
            record = session.scalar(select(Record))
            assert record.input_version == 2
            assert session.scalar(select(Candidate).where(Candidate.input_version == 2)) is None
        result = client.post("/desktop/manual/R001")
        assert result.status_code == 200, result.text
        with ws.factory() as session:
            candidate = session.scalar(select(Candidate).where(Candidate.input_version == 2))
            assert candidate.content["items"] == original_content["items"]
            assert candidate.provenance["source_candidate_id"] == original_id
            assert candidate.provenance["method"] == "manual_attachment_update"
            assert all(not p["reviewed"] for p in candidate.content["pages"])
            assert candidate.content["pages"][-1]["source_record_id"] == "R001-A1"
            assert candidate.content["issues"][-1]["code"] == "attachment_added_after_transcription"
            assert session.get(Candidate, original_id).content == original_content
            assert list(session.scalars(select(ExtractionRun))) == []
        # The same explicit manual action is idempotent.
        assert client.post("/desktop/manual/R001").json() == {"reused": True}
        owner = client.get("/api/me").json()["id"]
        service = app.state.review
        day = service.queue(owner)[0]["record_id"]
        service.assign(owner, day, owner)
        payload = service.detail(owner, day)
        assert any(p["source_record_id"] == "R001-A1" for p in payload["content"]["pages"])
        from food_record_digitizer.desktop_export import tables
        from food_record_digitizer.offline import OfflineManager
        from food_record_digitizer.offline_archive import unpack

        source_rows = tables([payload])["Sources"]
        attachment_row = next(row for row in source_rows if row["Source ID"] == "R001-A1")
        assert attachment_row["Attachment origin"] == "participant_material"
        assert attachment_row["Original attachment SHA256"]
        invitation = service.invite(owner, "attachment-reviewer@example.org", "Attachment reviewer")
        service.accept_invite(invitation["token"], "test-review-password")
        exported = OfflineManager(ws).export(owner, invitation["user_id"], [day])[0]
        manifest, blobs = unpack(Path(exported["path"]).read_bytes(), "task")
        unit = manifest["units"][0]
        assert (
            unit["snapshot"]["documents"]["R001-A1"]["attachment_original"]["origin"]
            == "participant_material"
        )
        assert any(page["source_record_id"] == "R001-A1" for page in unit["content"]["pages"])
        assert any(
            issue["code"] == "attachment_added_after_transcription" for issue in unit["content"]["issues"]
        )
    finally:
        app.state.queue.close()
