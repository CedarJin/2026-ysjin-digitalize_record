# ruff: noqa: F811
import copy
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_day_review import add_source, day_review  # noqa: F401
from test_ingest import ws  # noqa: F401

from food_record_digitizer import db
from food_record_digitizer.offline import OfflineManager
from food_record_digitizer.offline_archive import pack, unpack
from food_record_digitizer.review import ReviewError


def result_for(exported):
    manifest, _ = unpack(Path(exported["path"]).read_bytes(), "task")
    from food_record_digitizer.offline_archive import sha256

    results = []
    for unit in manifest["units"]:
        content = copy.deepcopy(unit["content"])
        for page in content["pages"]:
            page["reviewed"] = True
            page["regions"] = ["Whole page"]
            page["notes"] = "Checked; no change to this day’s food record"
        results.append(
            {"task_id": unit["task_id"], "baseline_sha256": unit["baseline_sha256"], "content": content}
        )
    return {
        "format_version": 1,
        "kind": "result",
        "package_id": manifest["package_id"],
        "project_id": manifest["project_id"],
        "reviewer_id": manifest["reviewer"]["id"],
        "task_archive_sha256": sha256(Path(exported["path"]).read_bytes()),
        "units": results,
    }


def test_export_return_duplicate_and_approval(day_review):
    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    assert manager.export(owner, a, [rid], preview=True)[0]["days"] == 1
    with svc.factory() as s:
        assert s.scalar(select(func.count()).select_from(db.DayReviewTask)) == 0
    exported = manager.export(owner, a, [rid])[0]
    manifest, blobs = unpack(Path(exported["path"]).read_bytes(), "task")
    assert len(blobs) >= 2
    assert manifest["previews"]
    assert "password" not in str(manifest)
    result = result_for(exported)
    data = pack(result, [])
    receipt = manager.import_result(owner, data)
    assert receipt["tasks"][0]["status"] == "accepted"
    assert manager.import_result(owner, data)["status"] == "duplicate"
    assert svc.detail(owner, rid)["approved_id"] is None
    svc.approve(owner, rid)
    release = svc.release(owner, [rid])
    assert release
    with svc.factory() as s:
        assert s.scalar(select(func.count()).select_from(db.DayReviewSubmission)) == 1


def test_old_result_retained_without_submission(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    exported = manager.export(owner, a, [rid])[0]
    add_source(svc.workspace, tmp_path, "R002")
    receipt = manager.import_result(owner, pack(result_for(exported), []))
    assert receipt["tasks"][0]["status"] == "stale"
    with svc.factory() as s:
        assert s.scalar(select(func.count()).select_from(db.DayReviewSubmission)) == 0
        assert s.scalar(select(func.count()).select_from(db.OfflineReceipt)) == 1
    assert list((svc.workspace.path / "offline/results").glob("*.zip"))


def test_independent_exports_conflict_and_tamper(day_review):
    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    first = manager.export(owner, a, [rid])[0]
    second = manager.export(owner, b, [rid])[0]
    m1, _ = unpack(Path(first["path"]).read_bytes(), "task")
    m2, _ = unpack(Path(second["path"]).read_bytes(), "task")
    assert m1["units"][0]["content"] == m2["units"][0]["content"]
    assert m1["units"][0]["task_id"] != m2["units"][0]["task_id"]
    result = result_for(first)
    tampered = copy.deepcopy(result)
    tampered["units"][0]["baseline_sha256"] = "wrong"
    with pytest.raises(ReviewError, match="baseline"):
        manager.import_result(owner, pack(tampered, []))
    with pytest.raises(ReviewError):
        manager.import_result(a, pack(result, []))
    manager.import_result(owner, pack(result, []))
    result["units"][0]["content"]["notes"] = "Different returned result"
    assert manager.import_result(owner, pack(result, []))["tasks"][0]["status"] == "conflict"
    with svc.factory() as s:
        assert s.scalar(select(func.count()).select_from(db.DayReviewSubmission)) == 1


def test_export_atomic_invalid_scope_and_offline_member(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    reviewer = manager.reviewer(owner, "Offline Person", "offline@example.org")
    assert manager.reviewer(owner, "Offline Person", "offline@example.org") == reviewer
    rid = svc.queue(owner)[0]["record_id"]
    with pytest.raises(ReviewError):
        manager.export(owner, reviewer, [rid, "unknown"])
    with svc.factory() as s:
        assert s.scalar(select(func.count()).select_from(db.DayReviewTask)) == 0
    for i in range(2, 24):
        add_source(svc.workspace, tmp_path, f"R{i:03}", date=f"2026-01-{i:02}")
    ids = [u["record_id"] for u in svc.queue(owner)]
    exports = manager.export(owner, reviewer, ids)
    assert [x["days"] for x in exports] == [10, 10, 3]


def test_regrouped_day_return_is_stale_not_lost(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    exported = manager.export(owner, a, [rid])[0]
    add_source(svc.workspace, tmp_path, "R001", date="2026-02-01")
    assert manager.import_result(owner, pack(result_for(exported), []))["tasks"][0]["status"] == "stale"


def test_invalid_second_result_rolls_back_entire_package(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    add_source(svc.workspace, tmp_path, "R002", date="2026-01-02")
    ids = [u["record_id"] for u in svc.queue(owner)]
    result = result_for(manager.export(owner, a, ids)[0])
    result["units"][1]["content"]["pages"][0]["reviewed"] = False
    with pytest.raises(ReviewError):
        manager.import_result(owner, pack(result, []))
    with svc.factory() as s:
        assert s.scalar(select(func.count()).select_from(db.DayReviewSubmission)) == 0
        assert s.scalar(select(func.count()).select_from(db.OfflineReceipt)) == 0


def test_manager_web_export_and_return(day_review):
    from fastapi.testclient import TestClient

    from food_record_digitizer.review_web import create_app

    svc, owner, a, b, h = day_review
    browser = TestClient(create_app(svc.workspace))
    browser.post("/api/login", json={"email": "owner@example.org", "password": "owner-password-123"})
    csrf = browser.get("/api/me").json()["csrf"]
    headers = {"x-csrf-token": csrf}
    rid = browser.get("/api/records").json()[0]["record_id"]
    body = {"reviewer_id": a, "unit_ids": [rid], "units_per_package": 1}
    assert browser.post("/api/offline/export", json=body).status_code == 403
    preview = browser.post("/api/offline/export", json={**body, "preview": True}, headers=headers)
    assert preview.status_code == 200 and preview.json()[0]["days"] == 1
    exported = browser.post("/api/offline/export", json=body, headers=headers).json()[0]
    assert "path" not in exported
    listing = browser.get("/api/offline/packages").json()
    assert len(listing) == 1
    download = browser.get("/api/offline/packages/" + exported["package_id"] + "/download")
    assert download.status_code == 200
    from food_record_digitizer.offline_reviewer import OfflineReviewer

    local = OfflineReviewer(svc.workspace.path / "test-reviewer", download.content)
    d = local.detail(rid)
    for page in d["content"]["pages"]:
        page["reviewed"] = True
        page["regions"] = ["Whole page"]
    saved = local.save(d["task"]["id"], d["task"]["revision"], d["content"])
    local.save(d["task"]["id"], saved["revision"], None, submit=True)
    result = local.export_result()
    response = browser.post(
        "/api/offline/import-result",
        files={"file": ("results.zip", result, "application/zip")},
        headers=headers,
    )
    assert response.status_code == 200 and response.json()["tasks"][0]["status"] == "accepted"
    assert (
        browser.post(
            "/api/offline/import-result",
            files={"file": ("results.zip", result, "application/zip")},
            headers=headers,
        ).json()["status"]
        == "duplicate"
    )
    reviewer = TestClient(create_app(svc.workspace))
    reviewer.post("/api/login", json={"email": "alice@example.org", "password": "review-password-123"})
    assert reviewer.get("/api/offline/packages").status_code == 403
    assert reviewer.get("/api/offline/packages/" + exported["package_id"] + "/download").status_code == 403


def test_settings_and_backup_preserve_exchange(day_review, tmp_path):
    from food_record_digitizer.backup import create_backup, restore_backup
    from food_record_digitizer.workspace import Workspace

    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    first = manager.export(owner, a, [rid], units_per_package=3)[0]
    second = manager.export(owner, b, [rid])[0]
    manifest, _ = unpack(Path(second["path"]).read_bytes(), "task")
    assert manifest["units_per_package"] == 3
    result = pack(result_for(first), [])
    manager.import_result(owner, result)
    backup = tmp_path / "backup.zip"
    create_backup(svc.workspace, backup)
    restore_backup(backup, tmp_path / "restored")
    restored = OfflineManager(Workspace(tmp_path / "restored"))
    assert restored.import_result(owner, result)["status"] == "duplicate"
    assert (tmp_path / "restored/offline/packages" / Path(first["path"]).name).read_bytes() == Path(
        first["path"]
    ).read_bytes()


def test_receipt_visibility_excludes_observer_from_independent_review(day_review):
    from fastapi.testclient import TestClient

    from food_record_digitizer.review_web import create_app

    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    exported = manager.export(owner, a, [rid])[0]
    receipt = manager.import_result(owner, pack(result_for(exported), []))
    browser = TestClient(create_app(svc.workspace))
    browser.post("/api/login", json={"email": "owner@example.org", "password": "owner-password-123"})
    route = "/api/offline/receipts/" + receipt["receipt_id"]
    assert browser.get(route).status_code == 200
    assert browser.get(route + "/download").status_code == 200
    with pytest.raises(ReviewError, match="seen"):
        manager.export(owner, owner, [rid])


def test_attachment_bundle_two_independent_returns(day_review, tmp_path):
    from test_ingest import pdf

    from food_record_digitizer.ingest import ingest_batch
    from food_record_digitizer.offline_reviewer import OfflineReviewer
    from food_record_digitizer.review import DEFAULT_POLICY

    svc, owner, a, b, h = day_review
    ingest_batch(svc.factory, svc.workspace.store, svc.project_id, [pdf(tmp_path / "R001-A1.pdf")], [])
    svc.set_policy(
        owner, {**DEFAULT_POLICY, "review_count": 2, "sample_percent": 0, "high_risk_flags": False}
    )
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    for reviewer_id in [a, b]:
        exported = manager.export(owner, reviewer_id, [rid])[0]
        local = OfflineReviewer(tmp_path / reviewer_id, Path(exported["path"]).read_bytes())
        detail = local.detail(rid)
        assert set(detail["snapshot"]["documents"]) == {"R001", "R001-A1"}
        assert len(detail["content"]["pages"]) == 2
        content = detail["content"]
        for page in content["pages"]:
            page["reviewed"] = True
            page["regions"] = ["Whole page"]
            page["notes"] = "Checked; no change to this day’s food record"
        saved = local.save(detail["task"]["id"], detail["task"]["revision"], content)
        local.save(detail["task"]["id"], saved["revision"], None, submit=True)
        manager.import_result(owner, local.export_result())
        if reviewer_id == a:
            with pytest.raises(ReviewError):
                svc.approve(owner, rid)
    svc.approve(owner, rid)
    assert svc.release(owner, [rid])


def test_unknown_dates_are_separate_exportable_and_approvable(day_review, tmp_path):
    from food_record_digitizer.offline_reviewer import OfflineReviewer

    svc, owner, a, b, h = day_review
    for rid in ['R002', 'R003']:
        add_source(svc.workspace, tmp_path, rid, date=None, dates=['2026-01-01'])
    rows = svc.queue(owner)
    unknown = [r for r in rows if r['date'] is None]
    assert len(unknown) == 2
    assert all(len(r['source_record_ids']) == 1 for r in unknown)
    assert not any(p['status'] == 'needs_confirmation' for p in svc.placements(owner))
    manager = OfflineManager(svc.workspace)
    exported = manager.export(owner, a, [r['record_id'] for r in unknown])[0]
    package = Path(exported['path']).read_bytes()
    local = OfflineReviewer(tmp_path / 'undated-review', package)
    assert local
    returned = result_for(exported)
    assert all(u['content']['days'][0]['date']['normalized'] is None for u in returned['units'])
    receipt = manager.import_result(owner, pack(returned, []))
    assert all(t['status'] == 'accepted' for t in receipt['tasks'])
    for row in unknown:
        svc.approve(owner, row['record_id'])


def test_metadata_only_record_not_in_review_queue(day_review):
    svc, owner, a, b, h = day_review
    with svc.factory.begin() as s:
        s.add(db.Record(project_id=svc.project_id, record_id='R999', state='missing_source'))
    assert len(svc.queue(owner)) == 1
    assert all(p['record_id'] != 'R999' for p in svc.placements(owner))
