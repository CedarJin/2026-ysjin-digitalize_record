# ruff: noqa: F811
import copy
import csv
import io
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select
from test_day_review import day_review, finish  # noqa: F401
from test_ingest import ws  # noqa: F401

from food_record_digitizer import db
from food_record_digitizer.desktop_export import tables
from food_record_digitizer.offline import OfflineManager
from food_record_digitizer.offline_reviewer import OfflineReviewer
from food_record_digitizer.review import ReviewError
from food_record_digitizer.review_web import release_zip


def replace_day(svc, value, needs=False):
    with svc.factory.begin() as s:
        candidate = s.scalar(select(db.Candidate))
        content = copy.deepcopy(candidate.content)
        content["days"][0]["date"] = value
        content["days"][0]["date_needs_confirmation"] = needs
        candidate.content = content


def test_relative_date_stays_unknown_until_reviewer_confirms(day_review):
    svc, owner, reviewer, _, _ = day_review
    replace_day(svc, {"raw": "Day two", "normalized": None, "missing_reason": "ambiguous"}, True)
    rid = svc.queue(owner)[0]["record_id"]
    detail = svc.detail(owner, rid)
    assert detail["date"] is None
    assert detail["content"]["days"][0]["date"]["raw"] == "Day two"
    task = svc.assign(owner, rid, reviewer)
    with pytest.raises(ReviewError, match="Date needs confirmation"):
        finish(svc, reviewer, task)
    detail = svc.detail(reviewer, rid)
    content = detail["content"]
    content["days"][0]["date_needs_confirmation"] = False
    content["days"][0]["date"]["missing_reason"] = "not_recorded"
    saved = svc.save(reviewer, task, detail["task"]["revision"], content)
    svc.submit(reviewer, task, saved["revision"])
    svc.approve(owner, rid)


def test_explicit_date_conflict_requires_confirmation_and_keeps_metadata(day_review):
    svc, owner, reviewer, _, _ = day_review
    with svc.factory() as s:
        ev = copy.deepcopy(s.scalar(select(db.Candidate)).content["items"][0]["evidence"])
    replace_day(svc, {"normalized": "2026-01-02", "evidence": ev})
    row = svc.queue(owner)[0]
    assert row["date"] == "2026-01-02"
    d = svc.detail(owner, row["record_id"])
    assert d["snapshot"]["metadata"]["R001"]["date"] == "2026-01-01"
    assert d["content"]["days"][0]["date_needs_confirmation"]
    assert any(i["code"] == "date_needs_confirmation" for i in d["content"]["issues"])


def test_unchanged_legacy_assignment_keeps_saved_review(day_review):
    svc, owner, reviewer, _, _ = day_review
    rid = svc.queue(owner)[0]["record_id"]
    with svc.factory.begin() as s:
        p = s.scalar(select(db.DayPlacement))
        p.status = "metadata_assigned"
        value = dict(p.proposed)
        value.pop("date_needs_confirmation", None)
        p.proposed = value
    svc.queue(owner)
    task = svc.assign(owner, rid, reviewer)
    finish(svc, reviewer, task)
    svc.approve(owner, rid)
    for _ in range(2):
        assert svc.detail(owner, rid)["approved_id"]
    with svc.factory() as s:
        assert s.get(db.DayReviewTask, task).status == "submitted"


def test_offline_date_correction_round_trip_and_research_export(day_review, tmp_path):
    svc, owner, reviewer, _, _ = day_review
    rid = svc.queue(owner)[0]["record_id"]
    manager = OfflineManager(svc.workspace)
    package = manager.export(owner, reviewer, [rid])[0]
    local = OfflineReviewer(tmp_path / "review", Path(package["path"]).read_bytes())
    detail = local.detail(rid)
    content = detail["content"]
    original = copy.deepcopy(content["days"][0])
    content["days"].append(
        {
            "day_id": "corrected",
            "date": {"normalized": "2026-01-03", "evidence": copy.deepcopy(content["items"][0]["evidence"])},
        }
    )
    content["events"][0]["day_id"] = "corrected"
    for page in content["pages"]:
        page["reviewed"] = True
        page["regions"] = ["Items in this review only"]
    saved = local.save(detail["task"]["id"], detail["task"]["revision"], content)
    assert saved["content"]["days"][0] == original
    local.save(detail["task"]["id"], saved["revision"], None, submit=True)
    result = local.export_result()
    receipt = manager.import_result(owner, result)
    assert all(t["status"] == "accepted" for t in receipt["tasks"])
    svc.approve(owner, rid)
    release = svc.release(owner, [rid])
    with svc.factory() as s:
        frozen = s.get(db.ReviewRelease, release)
        archive = zipfile.ZipFile(io.BytesIO(release_zip(frozen)))
        foods = list(csv.DictReader(io.StringIO(archive.read("food_items.csv").decode())))
        assert foods[0]["intake_date"] == foods[0]["date"] == "2026-01-03"
        assert foods[0]["review_group_date"] == "2026-01-01"
        flat = tables(frozen.payload["records"])["Food items"][0]
        assert flat["Intake date"] == "2026-01-03"
        assert flat["Review group date"] == "2026-01-01"
