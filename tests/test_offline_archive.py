import io
import json
import stat
import zipfile

import pytest

from food_record_digitizer.offline_archive import batch_units, pack, sha256, unpack
from food_record_digitizer.review import ReviewError


def test_stable_whole_day_batches():
    units = [
        {"id": str(i), "subject_id": "S1", "date": f"2026-01-{i:02}", "sources": ["main", "attachment"]}
        for i in range(1, 24)
    ]
    batches = batch_units(list(reversed(units)))
    assert [len(b) for b in batches] == [10, 10, 3]
    assert [u for b in batches for u in b] == units
    for bad in [0, -1, True, False, 1.5, "10", None]:
        with pytest.raises(ReviewError):
            batch_units(units, bad)
    with pytest.raises(ReviewError):
        batch_units([units[0], units[0]])


def test_archive_round_trip_deduplicates_and_is_repeatable():
    manifest = {"kind": "task", "format_version": 1, "reviewer": "Example"}
    source = b"synthetic scan"
    data = pack(manifest, [source, source])
    assert data == pack(manifest, [source])
    restored, blobs = unpack(data, "task")
    assert restored["reviewer"] == "Example"
    assert blobs == {"blobs/" + sha256(source): source}
    with pytest.raises(ReviewError):
        unpack(data, "result")


def malicious(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            z.writestr(info, data)
    return out.getvalue()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/file", "a\\b", "a/../b", "script.py"])
def test_unsafe_archive_names_rejected(name):
    with pytest.raises(ReviewError):
        unpack(malicious([(name, b"x", stat.S_IFREG)]), "task")


def test_symlink_duplicate_json_and_corrupt_blob_rejected():
    blob = "blobs/" + sha256(b"original")
    manifest = {
        "kind": "task",
        "format_version": 1,
        "files": {blob: {"sha256": sha256(b"original"), "bytes": 8}},
    }
    for entries in [
        [(blob, b"target", stat.S_IFLNK)],
        [("manifest.json", b'{"kind":"task","kind":"result"}', stat.S_IFREG)],
        [("manifest.json", json.dumps(manifest).encode(), stat.S_IFREG), (blob, b"modified", stat.S_IFREG)],
    ]:
        with pytest.raises(ReviewError):
            unpack(malicious(entries), "task")


def test_archive_size_limits_and_unlisted_files(monkeypatch):
    import food_record_digitizer.offline_archive as module

    data = pack({"kind": "task", "format_version": 1}, [b"12345678"])
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 4)
    with pytest.raises(ReviewError):
        unpack(data, "task")
    with pytest.raises(ReviewError):
        pack({"kind": "task", "format_version": 1}, [b"12345678"])
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 1024 * 1024)
    manifest = {"kind": "task", "format_version": 1, "files": {}}
    with pytest.raises(ReviewError):
        unpack(
            malicious(
                [
                    ("manifest.json", json.dumps(manifest).encode(), stat.S_IFREG),
                    ("blobs/" + sha256(b"x"), b"x", stat.S_IFREG),
                ]
            ),
            "task",
        )


def test_application_bundle_has_actionable_error():
    data = malicious([
        ("Offline Reviewer/start_reviewer.py", b"launcher", stat.S_IFREG),
    ])
    with pytest.raises(ReviewError, match="reviewer application, not a review task"):
        unpack(data, "task")


def test_repacked_archive_explains_original_zip_requirement():
    data = malicious([("__MACOSX/extra", b"metadata", stat.S_IFREG)])
    with pytest.raises(ReviewError, match="do not unzip and repackage"):
        unpack(data, "task")


def test_task_filename_uses_reviewer_and_safe_unique_suffix():
    from food_record_digitizer.offline_archive import task_filename

    manifest = {"reviewer": {"name": "Alice Chen"}, "package_id": "a1b2c3d4-rest"}
    assert task_filename(manifest) == "review-package-Alice-Chen-a1b2c3d4.zip"
    manifest["reviewer"]["name"] = "张三 / A: B\r\n"
    name = task_filename(manifest)
    assert "张三" in name
    assert not any(c in name for c in '/:\\;\r\n')
