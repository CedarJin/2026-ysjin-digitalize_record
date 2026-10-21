# ruff: noqa: F811
import json
import zipfile
from pathlib import Path

import pytest
from test_day_review import day_review  # noqa: F401
from test_ingest import ws  # noqa: F401
from test_offline import result_for

from food_record_digitizer.backup import create_backup
from food_record_digitizer.desktop_archive import inspect_archive
from food_record_digitizer.ingest import InputError
from food_record_digitizer.offline import OfflineManager
from food_record_digitizer.offline_archive import pack


def test_content_recognition_task_result_backup_and_corruption(day_review, tmp_path):
    svc, owner, a, _, _ = day_review
    exported = OfflineManager(svc.workspace).export(owner, a, [svc.queue(owner)[0]["record_id"]])[0]
    task = Path(exported["path"])
    assert inspect_archive(task)["kind"] == "task"
    result = tmp_path / "returned.zip"
    result.write_bytes(pack(result_for(exported), []))
    assert inspect_archive(result)["kind"] == "result"
    backup = tmp_path / "backup.zip"
    create_backup(svc.workspace, backup)
    assert inspect_archive(backup) == {"kind": "backup"}
    bad = tmp_path / "wrong.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"kind": "task", "format_version": 1, "files": {}}))
    with pytest.raises(InputError, match="validated"):
        inspect_archive(bad)
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("Food Record.app/Contents/Info.plist", "installer")
    with pytest.raises(InputError, match="app installer"):
        inspect_archive(bad)
    bad.write_bytes(b"not a zip")
    with pytest.raises(InputError, match="damaged"):
        inspect_archive(bad)


def test_bad_backup_rejected_before_restore(ws, tmp_path):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "backup_manifest.json", json.dumps({"records.db": "wrong", "workspace.json": "wrong"})
        )
        archive.writestr("records.db", b"not a database")
        archive.writestr("workspace.json", "{}")
    with pytest.raises(InputError, match="integrity"):
        inspect_archive(path)
