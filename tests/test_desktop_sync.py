# ruff: noqa: F811

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select
from test_ingest import pdf, ws  # noqa: F401

from food_record_digitizer import db
from food_record_digitizer.desktop_server import study_app
from food_record_digitizer.desktop_sync import scan, settings
from food_record_digitizer.ingest import InputError
from food_record_digitizer.workspace import Workspace


def metadata(path, rows):
    book = Workbook()
    sheet = book.active
    sheet.append(["filename", "participant", "date"])
    for row in rows:
        sheet.append(row)
    book.save(path)


def confirm(client, draft):
    report = client.post("/desktop/import/preview", json=draft)
    assert report.status_code == 200, report.text
    result = client.post("/desktop/import/confirm", json={**draft, "allow_update": True})
    assert result.status_code == 200, result.text
    return report.json(), result.json()


def test_sync_add_change_metadata_and_deletion_preserve_ids_without_ai(ws, tmp_path):
    folder = tmp_path / "inputs"
    folder.mkdir()
    pdf(folder / "diary.pdf")
    metadata(folder / "metadata.xlsx", [("diary.pdf", "P1", "2026-01-01")])
    app = study_app(ws, "secret")
    with TestClient(app, headers={"x-desktop-capability": "secret"}) as client:
        draft = client.post("/desktop/sync-folder", json={"folder": str(folder)}).json()
        rid = draft["entries"][0]["source_id"]
        _, result = confirm(client, draft)
        assert result["changed"] == [rid]
        # A reopened Study remembers both the directory and source association.
        draft = scan(Workspace(ws.root))
        assert draft["entries"][0]["source_id"] == rid
        report, result = confirm(client, draft)
        assert result["changed"] == [] and result["unchanged"] == [rid]
        # Changed PDF bytes and metadata retain the same identity.
        with (folder / "diary.pdf").open("ab") as f:
            f.write(b"\n% updated scan\n")
        pdf(folder / "new diary.pdf")
        metadata(
            folder / "metadata.xlsx",
            [("diary.pdf", "P1", "2026-01-02"), ("new diary.pdf", "P2", "2026-01-03")],
        )
        draft = scan(ws)
        assert next(e for e in draft["entries"] if e["filename"] == "diary.pdf")["source_id"] == rid
        # Explicit filename metadata makes the new file a distinct source, even if blank PDF bytes match.
        for entry in draft["entries"]:
            if entry["filename"] == "new diary.pdf":
                assert entry["source_id"] != rid
        report, result = confirm(client, draft)
        assert rid in report["revised_records"] and len(result["changed"]) == 2
        (folder / "diary.pdf").unlink()
        metadata(folder / "metadata.xlsx", [("new diary.pdf", "P2", "2026-01-03")])
        draft = scan(ws)
        assert draft["sync_missing_files"] == ["diary.pdf"]
        confirm(client, draft)
        with ws.factory() as session:
            assert len(list(session.scalars(select(db.Record)))) == 2
            assert not list(session.scalars(select(db.ExtractionRun)))
        assert client.get("/desktop/state").json()["sync_folder"] == str(folder)


def test_changed_files_after_sync_preview_cannot_be_confirmed(ws, tmp_path):
    folder = tmp_path / "inputs"
    folder.mkdir()
    pdf(folder / "R001.pdf")
    metadata(folder / "metadata.xlsx", [("R001.pdf", "P1", "2026-01-01")])
    app = study_app(ws, "secret")
    with TestClient(app, headers={"x-desktop-capability": "secret"}) as client:
        draft = scan(ws, str(folder))
        assert client.post("/desktop/import/preview", json=draft).status_code == 200
        metadata(folder / "metadata.xlsx", [("R001.pdf", "P1", "2026-02-01")])
        result = client.post("/desktop/import/confirm", json=draft)
        assert result.status_code == 400 and "changed after preview" in result.json()["error"]
        with ws.factory() as session:
            assert not list(session.scalars(select(db.Record)))


def test_sync_supports_data_folder_and_skips_managed_copies(tmp_path):
    workspace = Workspace.initialize(tmp_path / "study", "S", "Study", organized=True)
    data = workspace.root / "data"
    pdf(data / "R001.pdf")
    pdf(data / "sources" / "archived.pdf")
    metadata(data / "metadata.xlsx", [("R001.pdf", "P1", "2026-01-01")])
    (data / "~$metadata.xlsx").write_bytes(b"Excel lock")
    draft = scan(workspace, str(data))
    assert [e["filename"] for e in draft["entries"]] == ["R001.pdf"]
    with pytest.raises(InputError, match="app-managed"):
        scan(workspace, str(data / "sources"))


def test_multiple_metadata_choice_missing_folder_and_no_row_order_guess(ws, tmp_path):
    folder = tmp_path / "inputs"
    folder.mkdir()
    pdf(folder / "R001.pdf")
    pdf(folder / "R002.pdf")
    (folder / "a.csv").write_text("participant,date\nP1,2026-01-01\nP2,2026-01-02\n")
    (folder / "b.csv").write_text("participant,date\nP3,2026-01-03\n")
    result = scan(ws, str(folder))
    assert len(result["choose_metadata"]) == 2
    draft = scan(ws, str(folder), str(folder / "a.csv"))
    assert all(not r["record_id"] for r in draft["metadata"])
    assert settings(ws)["metadata"] == str(folder / "a.csv")
    with pytest.raises(InputError, match="unavailable"):
        scan(ws, str(tmp_path / "missing"))
    (folder / "a.csv").unlink()
    with pytest.raises(InputError, match="metadata file is missing"):
        scan(ws)
    assert "entries" in scan(ws, str(folder), reset_metadata=True)


def test_manual_metadata_links_survive_row_reordering_but_changes_need_confirmation(ws, tmp_path):
    folder = tmp_path / "inputs"
    folder.mkdir()
    pdf(folder / "R001.pdf")
    pdf(folder / "R002.pdf")
    (folder / "metadata.csv").write_text("participant,date\nP1,2026-01-01\nP2,2026-01-02\n")
    app = study_app(ws, "secret")
    with TestClient(app, headers={"x-desktop-capability": "secret"}) as client:
        draft = scan(ws, str(folder))
        draft["metadata"][0]["record_id"] = "R001"
        draft["metadata"][1]["record_id"] = "R002"
        confirm(client, draft)
        (folder / "metadata.csv").write_text("participant,date\nP2,2026-01-02\nP1,2026-01-01\n")
        assert [r["record_id"] for r in scan(ws)["metadata"]] == ["R002", "R001"]
        (folder / "metadata.csv").write_text("participant,date\nP2,2026-01-03\nP1,2026-01-01\n")
        assert [r["record_id"] for r in scan(ws)["metadata"]] == [None, "R001"]


def test_metadata_only_sync_updates_existing_record_without_removing_pdf(ws, tmp_path):
    folder = tmp_path / "inputs"
    folder.mkdir()
    pdf(folder / "diary.pdf")
    metadata(folder / "metadata.xlsx", [("diary.pdf", "P1", "2026-01-01")])
    app = study_app(ws, "secret")
    with TestClient(app, headers={"x-desktop-capability": "secret"}) as client:
        draft = scan(ws, str(folder))
        rid = draft["entries"][0]["source_id"]
        confirm(client, draft)
        (folder / "diary.pdf").unlink()
        metadata(folder / "metadata.xlsx", [("diary.pdf", "P1", "2026-01-02")])
        draft = scan(ws)
        assert not draft["entries"] and draft["metadata"][0]["record_id"] == rid
        report, result = confirm(client, draft)
        assert result["changed"] == [rid]
        row = client.get("/desktop/state").json()["records"][0]
        assert row["pages"] == 1 and row["metadata"]["date"] == "2026-01-02"
