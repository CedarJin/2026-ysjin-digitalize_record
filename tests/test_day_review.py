import copy
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_extraction import sample
from test_ingest import pdf, ws  # noqa: F401

from food_record_digitizer import db
from food_record_digitizer.day_review import DayReviewService
from food_record_digitizer.ingest import ingest_batch, ingest_one
from food_record_digitizer.review import DEFAULT_POLICY, ReviewError, ReviewService
from food_record_digitizer.review_web import create_app, release_zip
from food_record_digitizer.schemas import Metadata, RecordContent


def add_source(ws, tmp_path, rid="R001", sid="S1", date="2026-01-01", dates=None, candidate=True):  # noqa: F811
    ingest_one(
        ws.factory,
        ws.store,
        ws.project_id,
        pdf(tmp_path / (rid + ".pdf")),
        Metadata(record_id=rid, subject_id=sid, date=date),
    )
    if not candidate:
        return
    raw = json.loads(json.dumps(sample()).replace("R001", rid))
    raw["subject"]["normalized"] = sid
    evidence = raw["subject"]["evidence"]
    raw["days"] = []
    raw["events"] = []
    raw["items"] = []
    for i, d in enumerate(dates or [date]):
        raw["days"].append({"day_id": f"d{i}", "date": {"normalized": d, "evidence": evidence}})
        raw["events"].append({"event_id": f"e{i}", "day_id": f"d{i}"})
        raw["items"].append(
            {
                "item_id": f"i{i}",
                "event_id": f"e{i}",
                "evidence": evidence,
                "description": {"normalized": f"Food {i}", "evidence": evidence},
            }
        )
    raw["pages"][0]["regions"] = ["All food rows"]
    raw["pages"][0]["reviewed"] = False
    content = RecordContent.model_validate(raw).model_dump(mode="json")
    with ws.factory.begin() as s:
        r = s.scalar(
            select(db.Record).where(db.Record.record_id == rid, db.Record.project_id == ws.project_id)
        )
        s.add(
            db.Candidate(
                record_pk=r.id, input_version=r.input_version, content=content, provenance={"synthetic": True}
            )
        )


@pytest.fixture
def day_review(ws, tmp_path):  # noqa: F811
    add_source(ws, tmp_path)
    svc = DayReviewService(ws)
    owner = svc.bootstrap("owner@example.org", "Owner", "owner-password-123")
    users = []
    for name in ["Alice", "Bob", "Helper"]:
        i = svc.invite(owner, name.lower() + "@example.org", name)
        svc.accept_invite(i["token"], "review-password-123")
        users.append(i["user_id"])
    svc.set_policy(owner, {**DEFAULT_POLICY, "sample_percent": 0, "high_risk_flags": False})
    return svc, owner, *users


def finish(svc, user, task):
    with svc.factory() as s:
        t = s.get(db.DayReviewTask, task)
        data = copy.deepcopy(t.draft)
        rev = t.revision
    for p in data["pages"]:
        p["reviewed"] = True
        p["regions"] = ["All intake for this date"]
        p["notes"] = "Checked against this date."
    for i in data["issues"]:
        i["resolution"] = "Compared all sources; distinct intake verified."
    saved = svc.save(user, task, rev, data)
    return svc.submit(user, task, saved["revision"])


def test_day_grouping_submission_release_and_new_same_day_invalidates(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    add_source(svc.workspace, tmp_path, "R002")
    add_source(svc.workspace, tmp_path, "R003", sid="S2")
    add_source(svc.workspace, tmp_path, "R004", date="2026-01-02")
    rows = svc.queue(owner)
    assert len(rows) == 3
    row = next(r for r in rows if r["source_record_ids"] == ["R001", "R002"])
    rid = row["record_id"]
    d = svc.detail(owner, rid)
    assert len(d["content"]["items"]) == 2
    assert {i["item_id"] for i in d["content"]["items"]} == {"R001::i0", "R002::i0"}
    assert len(d["snapshot"]["documents"]) == 2
    task = svc.assign(owner, rid, a)
    finish(svc, a, task)
    svc.approve(owner, rid)
    release = svc.release(owner, [rid])
    with svc.factory() as s:
        frozen = copy.deepcopy(s.get(db.ReviewRelease, release).payload)
        archive = zipfile.ZipFile(io.BytesIO(release_zip(s.get(db.ReviewRelease, release))))
        assert json.loads(archive.read("scope.json"))["review_unit_type"] == "participant_date"
        assert b"R002" in archive.read("food_items.csv")
    assert frozen["records"][0]["source_record_ids"] == ["R001", "R002"]
    add_source(svc.workspace, tmp_path, "R005")
    with pytest.raises(ReviewError):
        svc.release(owner, [rid])
    assert svc.detail(owner, rid)["approved_id"] is None
    assert len(svc.detail(owner, rid)["content"]["items"]) == 3
    with svc.factory() as s:
        assert s.get(db.ReviewRelease, release).payload == frozen
        assert s.get(db.DayReviewTask, task).status == "stale"


def test_metadata_overrides_scan_and_reimport_invalidates_review(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    add_source(svc.workspace, tmp_path, "R002", dates=["2026-01-01", "2026-01-02"])
    rows = svc.queue(owner)
    assert len(rows) == 1
    rid = rows[0]["record_id"]
    detail = svc.detail(owner, rid)
    assert not detail["snapshot"]["pending_placements"]
    assert len(detail["content"]["items"]) == 3
    assert detail["content"]["days"][0]["date"]["normalized"] == "2026-01-01"
    assert any(i["code"] == "metadata_source_difference" for i in detail["content"]["issues"])
    p = svc.placements(owner)[0]
    with pytest.raises(ReviewError, match="metadata"):
        svc.resolve_placement(owner, p["id"], p["revision"], "S1", "2026-01-03", "Scan date")
    task = svc.assign(owner, rid, a)
    finish(svc, a, task)
    svc.approve(owner, rid)
    add_source(svc.workspace, tmp_path, "R002", date="2026-01-03")
    assert len(svc.queue(owner)) == 2
    assert svc.detail(owner, rid)["approved_id"] is None
    with svc.factory() as session:
        assert session.get(db.DayReviewTask, task).status == "stale"


def test_undated_source_does_not_block_dated_review_and_manual_source_included(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    add_source(svc.workspace, tmp_path, "R002", date=None, dates=["2026-01-01"])
    rid = next(r["record_id"] for r in svc.queue(owner) if r["date"])
    task = svc.assign(owner, rid, a)
    finish(svc, a, task)
    svc.approve(owner, rid)
    add_source(svc.workspace, tmp_path, "R003", candidate=False)
    detail = svc.detail(owner, rid)
    assert "R003" in detail["snapshot"]["documents"]
    assert any(i["code"] == "manual_transcription_required" for i in detail["content"]["issues"])


def test_day_blindness_help_cas_and_attachment_invalidation(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    svc.set_policy(
        owner, {**DEFAULT_POLICY, "review_count": 2, "sample_percent": 0, "high_risk_flags": False}
    )
    rid = svc.queue(owner)[0]["record_id"]
    ta = svc.assign(owner, rid, a)
    tb = svc.assign(owner, rid, b)
    help_id = svc.request_help(a, ta, "Check this amount")
    svc.assign_help(owner, help_id, h)
    with pytest.raises(ReviewError):
        svc.assign(owner, rid, h)
    d = svc.detail(a, rid)
    svc.save(a, ta, d["task"]["revision"], d["content"])
    with pytest.raises(ReviewError, match="newer"):
        svc.save(a, ta, d["task"]["revision"], d["content"])
    finish(svc, a, ta)
    assert svc.detail(b, rid)["submissions"] == []
    with pytest.raises(ReviewError):
        svc.approve(owner, rid)
    finish(svc, b, tb)
    svc.approve(owner, rid)
    ingest_batch(svc.factory, svc.workspace.store, svc.project_id, [pdf(tmp_path / "R001-A1.pdf")], [])
    with pytest.raises(ReviewError):
        svc.release(owner, [rid])


def test_legacy_drafts_preserved_and_default_web_uses_days(day_review):
    svc, owner, a, b, h = day_review
    legacy = ReviewService(svc.workspace)
    oldtask = legacy.assign(owner, "R001", a)
    browser = TestClient(create_app(svc.workspace))
    assert (
        browser.post(
            "/api/login", json={"email": "owner@example.org", "password": "owner-password-123"}
        ).status_code
        == 200
    )
    rows = browser.get("/api/records").json()
    assert rows[0]["record_id"].startswith("day-")
    assert browser.get("/api/records/R001").status_code == 409
    csrf = browser.get("/api/me").json()["csrf"]
    rid = rows[0]["record_id"]
    assert browser.get(f"/api/records/{rid}/source/R001/1").status_code == 200
    assert browser.get("/api/grouping").status_code == 200
    assert svc.archive(a)[0]["task_id"] == oldtask
    assert svc.archive(b) == []
    with pytest.raises(ReviewError, match="not found"):
        svc.hide_archive(b, oldtask)
    saved = svc.archive(a)[0]["content"]
    svc.hide_archive(a, oldtask)
    svc.hide_archive(a, oldtask)
    assert svc.archive(a) == []
    with svc.factory() as s:
        assert s.get(db.ReviewTask, oldtask).draft == saved

    with pytest.raises(ReviewError, match="superseded"):
        svc.save(a, oldtask, 1, {})
    assert browser.post(
        "/api/assign", json={"record_ids": [rid], "assignee_id": a}, headers={"x-csrf-token": csrf}
    ).json()[0]["task_id"]


def test_manual_extra_date_invalidates_original_day_and_keeps_source_context(day_review):
    svc, owner, a, b, h = day_review
    rid = svc.queue(owner)[0]["record_id"]
    task = svc.assign(owner, rid, a)
    p = svc.placements(owner)[0]
    svc.add_source_day(
        owner, p["id"], p["revision"], "S1", "2026-01-02", "Additional date visible on original scan"
    )
    rows = svc.queue(owner)
    assert len(rows) == 2
    with svc.factory() as s:
        assert s.get(db.DayReviewTask, task).status == "stale"
    second = svc.detail(owner, next(r["record_id"] for r in rows if r["date"] == "2026-01-02"))
    assert not second["content"]["items"]
    assert "R001" in second["snapshot"]["documents"]
    assert any(i["code"] == "manual_transcription_required" for i in second["content"]["issues"])
    with pytest.raises(ReviewError, match="already"):
        svc.add_source_day(owner, p["id"], p["revision"], "S1", "2026-01-02", "Duplicate")


def test_same_day_late_ai_draft_invalidates_and_unit_identity_is_fixed(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    rid = svc.queue(owner)[0]["record_id"]
    task = svc.assign(owner, rid, a)
    d = svc.detail(a, rid)
    changed = copy.deepcopy(d["content"])
    changed["days"][0]["date"]["normalized"] = "2026-01-02"
    with pytest.raises(ReviewError, match="grouping"):
        svc.save(a, task, d["task"]["revision"], changed)
    add_source(svc.workspace, tmp_path, dates=["2026-01-01"])
    with pytest.raises(ReviewError, match="editable"):
        svc.save(a, task, d["task"]["revision"], d["content"])


def test_day_web_permissions_batch_and_grouping_cas(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    add_source(svc.workspace, tmp_path, "R002")
    with svc.factory() as s:
        batch = s.scalar(select(db.Batch).order_by(db.Batch.created_at.desc()))
        batch_id = batch.id
    browser = TestClient(create_app(svc.workspace))
    browser.post("/api/login", json={"email": "owner@example.org", "password": "owner-password-123"})
    rows = browser.get("/api/records?batch_id=" + batch_id).json()
    assert rows[0]["source_record_ids"] == ["R001", "R002"]
    rid = rows[0]["record_id"]
    assert browser.get(f"/api/records/{rid}/history").status_code == 200
    p = browser.get("/api/grouping").json()[0]
    body = {
        "revision": p["revision"],
        "subject_id": "S1",
        "date": "2026-01-01",
        "reason": "Confirm metadata",
    }
    assert browser.post("/api/grouping/" + p["id"], json=body).status_code == 403
    csrf = browser.get("/api/me").json()["csrf"]
    assert (
        browser.post("/api/grouping/" + p["id"], json=body, headers={"x-csrf-token": csrf}).status_code == 200
    )
    assert (
        browser.post("/api/grouping/" + p["id"], json=body, headers={"x-csrf-token": csrf}).status_code == 409
    )
    reviewer = TestClient(create_app(svc.workspace))
    reviewer.post("/api/login", json={"email": "alice@example.org", "password": "review-password-123"})
    assert reviewer.get("/api/grouping").status_code == 403
    assert reviewer.get(f"/api/grouping/{p['id']}/source/R001/1").status_code == 403
    assert reviewer.get(f"/api/records/{rid}").status_code == 403


def test_daily_reconciliation_reminders_need_no_individual_notes(day_review, tmp_path):
    svc, owner, a, b, h = day_review
    add_source(svc.workspace, tmp_path, "R002")
    rid = svc.queue(owner)[0]["record_id"]
    task = svc.assign(owner, rid, a)
    d = svc.detail(a, rid)
    data = d["content"]
    assert any(x["code"] == "same_day_reconciliation" for x in data["issues"])
    for p in data["pages"]:
        p["reviewed"] = True
        p["regions"] = ["All rows for this date"]
    assert all(x["resolution"] is None for x in data["issues"])
    saved = svc.save(a, task, d["task"]["revision"], data)
    svc.submit(a, task, saved["revision"])
    assert svc.approve(owner, rid)


def test_metadata_reference_cannot_forge_provenance(day_review):
    svc, owner, a, b, h = day_review
    rid = svc.queue(owner)[0]["record_id"]
    task = svc.assign(owner, rid, a)
    d = svc.detail(a, rid)
    data = d["content"]
    assert data["subject"]["metadata_reference"]["field"] == "subject_id"
    data["subject"]["metadata_reference"]["record_id"] = "NONEXISTENT"
    for p in data["pages"]:
        p["reviewed"] = True
        p["regions"] = ["Whole page"]
    saved = svc.save(a, task, d["task"]["revision"], data)
    svc.submit(a, task, saved["revision"])
    with pytest.raises(ReviewError, match="structural"):
        svc.approve(owner, rid)


def test_existing_conflict_migrates_once_without_changing_candidate(day_review):
    svc, owner, a, b, h = day_review
    svc.queue(owner)
    with svc.factory.begin() as s:
        placement = s.scalar(select(db.DayPlacement))
        placement.status = "needs_confirmation"
        placement.date = "2026-02-01"
        candidate = s.scalar(select(db.Candidate))
        original = copy.deepcopy(candidate.content)
        candidate_id = candidate.id
    rows = svc.queue(owner)
    assert len(rows) == 1 and rows[0]["date"] == "2026-01-01"
    with svc.factory() as s:
        p = s.scalar(select(db.DayPlacement))
        revision = p.revision
        assert p.status == "metadata_assigned"
        assert s.get(db.Candidate, candidate_id).content == original
    svc.queue(owner)
    with svc.factory() as s:
        assert s.scalar(select(db.DayPlacement)).revision == revision


def test_field_inheritance_survives_day_grouping_and_approval(day_review):
    svc, owner, reviewer, _, _ = day_review
    with svc.factory.begin() as s:
        candidate = s.scalar(select(db.Candidate))
        raw = copy.deepcopy(candidate.content)
        raw["items"][0]["unit"] = {"normalized": "cup", "inherited_from": "d0.date"}
        raw["events"][0]["place"] = {"normalized": "Home", "inherited_from": "i0.description"}
        candidate.content = RecordContent.model_validate(raw).model_dump(mode="json")
    rid = svc.queue(owner)[0]["record_id"]
    data = svc.detail(owner, rid)["content"]
    assert data["items"][0]["unit"]["inherited_from"] == "intake-day.date"
    assert data["events"][0]["place"]["inherited_from"] == "R001::i0.description"
    task = svc.assign(owner, rid, reviewer)
    finish(svc, reviewer, task)
    assert svc.approve(owner, rid)
