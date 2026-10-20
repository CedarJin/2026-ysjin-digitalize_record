# ruff: noqa: F811
import copy
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from test_ingest import ws  # noqa: F401
from test_review import completed, review  # noqa: F401

from food_record_digitizer.backup import create_backup, restore_backup
from food_record_digitizer.review import DEFAULT_POLICY, ReviewError, ReviewService
from food_record_digitizer.review_web import create_app
from food_record_digitizer.workspace import Workspace


def client(svc, email="owner@example.org", password="owner-password-123"):
    browser = TestClient(create_app(svc.workspace, service_factory=ReviewService))
    response = browser.post("/api/login", json={"email": email, "password": password})
    assert response.status_code == 200
    browser.headers["X-CSRF-Token"] = response.json()["csrf"]
    return browser


def test_http_authorization_source_csrf_and_release(review):
    svc, owner, alice, bob, _ = review
    task = svc.assign(owner, "R001", alice)
    guest = TestClient(create_app(svc.workspace, service_factory=ReviewService))
    assert guest.get("/api/records").status_code == 401
    assert guest.get("/api/records/R001/source/R001/1").status_code == 401
    a = client(svc, "alice@example.org", "review-password-123")
    b = client(svc, "bob@example.org", "review-password-123")
    assert b.get("/api/records").json() == []
    assert b.get("/api/records/R001").status_code == 403
    assert b.get("/api/records/R001/source/R001/1").status_code == 403
    assert a.get("/api/records/R001/source/R001/1").headers["content-type"] == "image/png"
    assert a.post("/api/approve", json={"record_ids": ["R001"]}).status_code == 403
    assert a.get("/api/members").status_code == 403
    assert a.post("/api/logout", json={}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert a.post("/api/logout", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    completed(svc, alice, task)
    svc.approve(owner, "R001")
    release = svc.release(owner, ["R001"])
    assert a.get(f"/api/releases/{release}/download").status_code == 403
    o = client(svc)
    response = o.get(f"/api/releases/{release}/download")
    assert response.status_code == 200
    assert o.get(f"/api/releases/{release}/download").content == response.content
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        assert json.loads(z.read("review_history.json"))[0]["submissions"]
        manifest = json.loads(z.read("export_manifest.json"))
        import hashlib

        assert all(hashlib.sha256(z.read(k)).hexdigest() == v for k, v in manifest["files"].items())
    svc.set_member_active(owner, alice, False)
    assert a.get("/api/records/R001/source/R001/1").status_code in {401, 403}


def test_http_multitab_conflict_blind_history_and_restart(review):
    svc, owner, alice, bob, _ = review
    svc.set_policy(
        owner, {**DEFAULT_POLICY, "review_count": 2, "high_risk_flags": False, "sample_percent": 0}
    )
    task = svc.assign(owner, "R001", alice)
    svc.assign(owner, "R001", bob)
    a = client(svc, "alice@example.org", "review-password-123")
    b = client(svc, "bob@example.org", "review-password-123")
    detail = a.get("/api/records/R001").json()
    payload = {"revision": detail["task"]["revision"], "content": detail["content"]}
    payload["content"]["notes"] = "Private first review"
    assert a.put("/api/tasks/" + task, json=payload).status_code == 200
    assert a.put("/api/tasks/" + task, json=payload).status_code == 409
    assert b.get("/api/records/R001/history").json()["edits"] == []
    assert b.get("/api/records/R001").json()["content"]["notes"] == ""
    restarted = ReviewService(Workspace(svc.workspace.path))
    assert restarted.detail(alice, "R001")["content"]["notes"] == "Private first review"
    completed(svc, alice, task)
    assert b.get("/api/records/R001").json()["submissions"] == []


def test_single_batch_import_and_late_attachment(review, tmp_path):
    svc, owner, alice, bob, _ = review
    from test_ingest import pdf

    o = client(svc)
    files = [
        ("files", ("R002.pdf", pdf(tmp_path / "R002.pdf").read_bytes(), "application/pdf")),
        ("files", ("R002-A1.pdf", pdf(tmp_path / "R002-A1.pdf").read_bytes(), "application/pdf")),
    ]
    preview = o.post(
        "/api/import/preview",
        files=files,
        data={"metadata_json": json.dumps([{"record_id": "R002", "subject_id": "S2"}])},
    )
    assert preview.status_code == 200, preview.text
    assert "R002" in preview.json()["report"]["groups"]
    result = o.post("/api/import/confirm", json={"token": preview.json()["token"]})
    assert result.status_code == 200, result.text
    assert "R002" in result.json()["changed"]
    duplicate = o.post("/api/import/confirm", json={"token": preview.json()["token"]})
    assert duplicate.json()["changed"] == []
    svc.assign(owner, "R002", alice)
    assert len(svc.detail(alice, "R002")["content"]["pages"]) == 2
    followup = o.post(
        "/api/import/preview",
        files={"files": ("R002-A2.pdf", pdf(tmp_path / "R002-A2.pdf").read_bytes(), "application/pdf")},
    )
    assert o.post("/api/import/confirm", json={"token": followup.json()["token"]}).status_code == 200
    assert svc.queue(alice)[0]["tasks"][0]["status"] == "stale"
    assert o.post("/api/import/preview", files={"files": ("../bad.pdf", b"%PDF-invalid")}).status_code == 400


def test_invites_expire_revoke_reset_and_invalid_requests(review):
    svc, owner, alice, bob, _ = review
    from food_record_digitizer.db import ReviewUser

    invite = svc.invite(owner, "new@example.org", "New")
    with svc.factory.begin() as s:
        s.get(ReviewUser, invite["user_id"]).invite_expires = "2000-01-01T00:00:00+00:00"
    with pytest.raises(ReviewError):
        svc.accept_invite(invite["token"], "a-long-password")
    token, _ = svc.login("alice@example.org", "review-password-123")
    replacement = svc.renew_invite(owner, alice)
    with pytest.raises(ReviewError):
        svc.authenticate(token)
    svc.accept_invite(replacement["token"], "a-new-password-123")
    with pytest.raises(ReviewError):
        svc.accept_invite(replacement["token"], "a-new-password-123")
    o = client(svc)
    assert o.post("/api/assign", json={}).status_code == 400
    assert o.post("/api/policy", json={"review_count": 2}).status_code == 400


def test_policy_invalidates_approval_backup_restore_and_coverage(review, tmp_path):
    svc, owner, alice, bob, _ = review
    task = svc.assign(owner, "R001", alice)
    completed(svc, alice, task)
    svc.approve(owner, "R001")
    rid = svc.release(owner, ["R001"])
    from food_record_digitizer.db import ReviewRelease

    with svc.factory() as s:
        payload = copy.deepcopy(s.get(ReviewRelease, rid).payload)
    token, _ = svc.login("alice@example.org", "review-password-123")
    archive = tmp_path / "backup.zip"
    run_file = svc.workspace.path / "runs" / "archived-run" / "manifest.json"
    run_file.parent.mkdir(parents=True)
    run_file.write_text('{"prompt_version": "test"}')
    create_backup(svc.workspace, archive)
    restore_backup(archive, tmp_path / "restored")
    restored = ReviewService(Workspace(tmp_path / "restored"))
    assert (
        restored.workspace.path / "runs" / "archived-run" / "manifest.json"
    ).read_bytes() == run_file.read_bytes()
    with pytest.raises(ReviewError):
        restored.authenticate(token)
    assert restored.detail(owner, "R001")["approved_id"]
    with restored.factory() as s:
        assert s.get(ReviewRelease, rid).payload == payload
    svc.set_policy(owner, {**DEFAULT_POLICY, "review_count": 2})
    with pytest.raises(ReviewError):
        svc.release(owner, ["R001"])
    assert svc.queue(owner)[0]["state"] == "pending_review"
    svc.exclude(owner, "R001", "Outside study inclusion criteria")
    svc.restore(owner, "R001", "Eligibility confirmed by coordinator")
    assert svc.queue(owner)[0]["state"] == "pending_review"
