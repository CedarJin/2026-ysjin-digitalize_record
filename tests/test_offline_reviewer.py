# ruff: noqa: F811
import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_day_review import day_review  # noqa: F401
from test_ingest import ws  # noqa: F401

from food_record_digitizer.offline import OfflineManager
from food_record_digitizer.offline_reviewer import OfflineReviewer
from food_record_digitizer.offline_web import create_offline_app
from food_record_digitizer.review import ReviewError


def test_local_resume_cas_submit_and_return(day_review, tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("No network"))
    )
    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    exported = manager.export(owner, a, [rid])[0]
    package = Path(exported["path"]).read_bytes()
    local = OfflineReviewer(tmp_path / "reviewer-computer", package)
    with pytest.raises(ReviewError, match="Complete every day"):
        local.export_result()
    detail = local.detail(rid)
    task = detail["task"]
    content = copy.deepcopy(detail["content"])
    content["items"][0]["description"]["normalized"] = "Corrected food"
    saved = local.save(task["id"], task["revision"], content)
    with pytest.raises(ReviewError, match="newer"):
        local.save(task["id"], task["revision"], content)
    reopened = OfflineReviewer(tmp_path / "reviewer-computer", package)
    assert reopened.detail(rid)["content"]["items"][0]["description"]["normalized"] == "Corrected food"
    content = reopened.detail(rid)["content"]
    for page in content["pages"]:
        page["reviewed"] = True
        page["regions"] = ["Whole page"]
        page["notes"] = "Checked; no change"
    saved = reopened.save(task["id"], saved["revision"], content)
    reopened.save(task["id"], saved["revision"], None, submit=True)
    result = reopened.export_result()
    assert reopened.export_result() == result
    receipt = manager.import_result(owner, result)
    assert receipt["tasks"][0]["status"] == "accepted"
    svc.approve(owner, rid)
    assert svc.release(owner, [rid])


def test_local_web_session_scope_and_source(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    exported = manager.export(owner, a, [rid])[0]
    local = OfflineReviewer(tmp_path / "local", Path(exported["path"]).read_bytes())
    app = create_offline_app(local, launch_token="test-capability")
    browser = TestClient(app)
    assert browser.get("/api/me").status_code == 401
    browser.get("/?launch=test-capability")
    me = browser.get("/api/me").json()
    assert me["offline"] and me["role"] == "reviewer"
    assert browser.get("/api/records").json()[0]["record_id"] == rid
    d = browser.get("/api/records/" + rid).json()
    assert browser.get("/api/records/unknown").status_code == 404
    assert browser.get("/api/members").status_code == 404
    body = {"revision": d["task"]["revision"], "content": d["content"]}
    route = "/api/tasks/" + d["task"]["id"]
    assert browser.put(route, json=body).status_code == 403
    assert (
        browser.put(
            route, json=body, headers={"x-csrf-token": me["csrf"], "origin": "https://untrusted.example"}
        ).status_code
        == 403
    )
    assert browser.put(route, json=body, headers={"x-csrf-token": me["csrf"]}).status_code == 200
    image = browser.get("/api/records/" + rid + "/source/R001/1")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert browser.get("/api/offline/result").status_code == 400


def test_browser_open_package_and_preview_without_poppler(day_review, tmp_path, monkeypatch):
    import shutil

    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    exported = manager.export(owner, a, [rid])[0]
    data = Path(exported["path"]).read_bytes()
    app = create_offline_app(workspace=tmp_path / "browser-reviewer", launch_token="local-launch")
    browser = TestClient(app)
    browser.get("/?launch=local-launch")
    me = browser.get("/api/me").json()
    assert me["package_id"] is None
    headers = {"x-csrf-token": me["csrf"]}
    response = browser.post(
        "/api/offline/open", files={"file": ("task.zip", data, "application/zip")}, headers=headers
    )
    assert response.status_code == 200
    assert browser.get("/api/me").json()["package_id"]
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert browser.get("/api/records/" + rid + "/source/R001/1").status_code == 200
    assert (
        browser.post(
            "/api/offline/open", files={"file": ("task.zip", data, "application/zip")}, headers=headers
        ).status_code
        == 409
    )


def test_reopen_resubmit_preserves_history_and_prior_return(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    package = Path(manager.export(owner, a, [rid])[0]["path"]).read_bytes()
    local = OfflineReviewer(tmp_path / "reopen", package)
    d = local.detail(rid)
    task = d["task"]["id"]
    content = d["content"]
    for page in content["pages"]:
        page.update(reviewed=True, regions=["Whole page"], notes="Checked")
    saved = local.save(task, d["task"]["revision"], content)
    submitted = local.save(task, saved["revision"], None, submit=True)
    original = local.export_result()
    assert manager.import_result(owner, original)["tasks"][0]["status"] == "accepted"
    app = create_offline_app(local, launch_token="reopen-capability")
    browser = TestClient(app)
    browser.get("/?launch=reopen-capability")
    headers = {"x-csrf-token": browser.get("/api/me").json()["csrf"]}
    route = "/api/tasks/" + task + "/reopen"
    body = {"revision": submitted["revision"]}
    assert browser.post(route, json=body).status_code == 403
    assert browser.post(route, json={"revision": True}, headers=headers).status_code == 400
    assert browser.post(route, json={"revision": saved["revision"]}, headers=headers).status_code == 409
    assert browser.post(route, json=body, headers=headers).status_code == 200
    assert browser.post(route, json=body, headers=headers).status_code == 409
    local = OfflineReviewer(tmp_path / "reopen", package)
    d = local.detail(rid)
    assert d["task"]["status"] == "in_progress"
    assert d["content"] == submitted["content"]
    with pytest.raises(ReviewError, match="Complete every day"):
        local.export_result()
    with pytest.raises(ReviewError):
        local.save(task, submitted["revision"], d["content"])
    d["content"]["items"][0]["description"]["normalized"] = "Later correction"
    saved = local.save(task, d["task"]["revision"], d["content"])
    local.save(task, saved["revision"], None, submit=True)
    revised = local.export_result()
    assert revised != original
    from food_record_digitizer.offline_archive import unpack

    manifest, _ = unpack(revised, "result")
    assert len(manifest["units"][0]["edits"]) == 5
    assert manager.import_result(owner, revised)["tasks"][0]["status"] == "conflict"
    assert manager.import_result(owner, original)["status"] == "duplicate"


@pytest.mark.parametrize("time,label", [("08:30", None), (None, "Breakfast"), (None, None)])
def test_time_and_label_are_independently_optional(day_review, tmp_path, time, label):
    svc, owner, a, b, h = day_review
    manager = OfflineManager(svc.workspace)
    rid = svc.queue(owner)[0]["record_id"]
    package = Path(manager.export(owner, a, [rid])[0]["path"]).read_bytes()
    local = OfflineReviewer(tmp_path / "optional-fields", package)
    d = local.detail(rid)
    content = d["content"]
    evidence = content["items"][0]["evidence"]
    for event in content["events"]:
        for key, value in [("time", time), ("meal_label", label)]:
            event[key] = {
                "raw": value, "normalized": value,
                "missing_reason": "not_recorded" if value is None else None,
                "evidence": copy.deepcopy(evidence) if value else [],
                "inherited_from": None,
            }
    for page in content["pages"]:
        page.update(reviewed=True, regions=["Whole page"], notes="Checked")
    task = d["task"]
    saved = local.save(task["id"], task["revision"], content)
    local.save(task["id"], saved["revision"], None, submit=True)
    result = local.export_result()
    from food_record_digitizer.offline_archive import unpack

    manifest, _ = unpack(result, "result")
    for event in manifest["units"][0]["content"]["events"]:
        assert event["time"]["normalized"] == time
        assert event["meal_label"]["normalized"] == label
    assert manager.import_result(owner, result)["tasks"][0]["status"] == "accepted"
    svc.approve(owner, rid)
