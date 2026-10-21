import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_day_review import add_source

from food_record_digitizer.backup import create_backup, restore_backup
from food_record_digitizer.db import Candidate, Record
from food_record_digitizer.desktop_server import study_app
from food_record_digitizer.ingest import InputError
from food_record_digitizer.study_layout import engine_path, organize
from food_record_digitizer.workspace import Workspace


def test_organized_workspace_sources_backup_restore_and_move(tmp_path):
    root = tmp_path / "Study"
    ws = Workspace.initialize(root, "study", "Study", organized=True)
    assert ws.path == root / ".app" and ws.root == root
    assert json.loads((root / "workspace.json").read_text())["storage_version"] == 2
    assert not (root / "records.db").exists()
    assert (root / ".app/records.db").is_file()
    add_source(ws, tmp_path)
    (ws.path / "runs/test-run").mkdir(parents=True)
    (ws.path / "runs/test-run/response.json").write_text('{"synthetic":true}')
    assert (root / "data/sources/records").exists() is False  # CLI source keeps its hashed original.
    assert list((root / "data/sources").rglob("*.pdf"))
    assert (root / "results/extractions/test-run/response.json").is_file()
    archive = tmp_path / "study.zip"
    create_backup(ws, archive)
    restored = tmp_path / "Restored"
    restore_backup(archive, restored)
    again = Workspace(restored)
    assert again.root == restored
    assert (restored / "results/extractions/test-run/response.json").read_text() == '{"synthetic":true}'
    assert list((restored / "data/sources").rglob("*.pdf"))
    moved = tmp_path / "Moved"
    again.factory.kw["bind"].dispose()
    shutil.move(restored, moved)
    opened = Workspace(moved)
    assert len(opened.records()) == 1
    assert (moved / ".app/sources").resolve() == moved / "data/sources"


def test_existing_study_copy_is_organized_without_changing_original(tmp_path):
    root = tmp_path / "Original"
    ws = Workspace.initialize(root, "old", "Old")
    add_source(ws, tmp_path)
    with ws.factory() as session:
        original = [(r.id, r.fingerprint, r.input_version) for r in session.scalars(select(Record))]
        candidate = session.scalar(select(Candidate)).content
    app = study_app(ws, "secret")
    try:
        client = TestClient(app, headers={"x-desktop-capability": "secret"})
        dest = tmp_path / "Organized copy"
        result = client.post("/desktop/organized-copy", json={"path": str(dest)})
        assert result.status_code == 200, result.text
        assert (root / "records.db").is_file() and not (root / ".app").exists()
        copied = Workspace(dest)
        with copied.factory() as session:
            assert [
                (r.id, r.fingerprint, r.input_version) for r in session.scalars(select(Record))
            ] == original
            assert session.scalar(select(Candidate)).content == candidate
        assert client.post("/desktop/organized-copy", json={"path": str(root / "nested")}).status_code == 400
        assert client.post("/desktop/organized-copy", json={"path": str(dest)}).status_code == 400
    finally:
        app.state.queue.close()


def test_layout_rejects_missing_internal_database(tmp_path):
    root = tmp_path / "Study"
    ws = Workspace.initialize(root, "study", "Study", organized=True)
    ws.factory.kw["bind"].dispose()
    (root / ".app/records.db").unlink()
    with pytest.raises(InputError, match="missing"):
        engine_path(root)


def test_organize_rolls_back_failed_move(tmp_path, monkeypatch):
    from food_record_digitizer import study_layout

    root = tmp_path / "Study"
    ws = Workspace.initialize(root, "study", "Study")
    ws.factory.kw["bind"].dispose()
    original = (root / "workspace.json").read_bytes()
    replace = study_layout.os.replace

    def fail(source, destination):
        if Path(source).name == "records.db":
            raise OSError("Synthetic disk failure")
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(study_layout.os, "replace", fail)
        with pytest.raises(OSError):
            organize(root)
    assert (root / "records.db").is_file()
    assert (root / "workspace.json").read_bytes() == original
    assert not (root / ".app").exists()
    assert Workspace(root).root == root
