import copy

import pytest
from sqlalchemy import select
from test_extraction import sample
from test_ingest import pdf, ws  # noqa: F401

from food_record_digitizer.db import Candidate, ReviewRelease, ReviewSubmission, ReviewTask
from food_record_digitizer.ingest import ingest_batch, ingest_one
from food_record_digitizer.review import DEFAULT_POLICY, ReviewError, ReviewService
from food_record_digitizer.schemas import Metadata, RecordContent


@pytest.fixture
def review(ws, tmp_path):  # noqa: F811
    ingest_one(
        ws.factory,
        ws.store,
        ws.project_id,
        pdf(tmp_path / "R001.pdf"),
        Metadata(record_id="R001", subject_id="S1"),
    )
    content = RecordContent.model_validate(sample()).model_dump(mode="json")
    content["pages"][0]["reviewed"] = False
    content["pages"][0]["regions"] = ["row 1"]
    with ws.factory.begin() as s:
        r = ws.records()[0]
        s.add(
            Candidate(
                record_pk=r.id, input_version=r.input_version, content=content, provenance={"test": True}
            )
        )
    svc = ReviewService(ws)
    owner = svc.bootstrap("owner@example.org", "Owner", "owner-password-123")
    users = []
    for name in ["Alice", "Bob", "Casey"]:
        invite = svc.invite(owner, name.lower() + "@example.org", name)
        svc.accept_invite(invite["token"], "review-password-123")
        users.append(invite["user_id"])
    svc.set_policy(owner, {**DEFAULT_POLICY, "sample_percent": 0, "high_risk_flags": False})
    return svc, owner, *users


def completed(svc, user, task):
    with svc.factory() as s:
        t = s.get(ReviewTask, task)
        data = copy.deepcopy(t.draft)
        revision = t.revision
    for page in data["pages"]:
        page["reviewed"] = True
    saved = svc.save(user, task, revision, data, "Checked against the scan")
    return svc.submit(user, task, saved["revision"])


def test_review_approve_release_preserves_old_snapshot_after_new_attachment(review, tmp_path):
    svc, owner, a, b, c = review
    task = svc.assign(owner, "R001", a)
    completed(svc, a, task)
    approval = svc.approve(owner, "R001")
    release = svc.release(owner, ["R001"])
    with svc.factory() as s:
        original = copy.deepcopy(s.get(ReviewRelease, release).payload)
        assert original["records"][0]["approval_id"] == approval
    ingest_batch(svc.factory, svc.workspace.store, svc.project_id, [pdf(tmp_path / "R001-A1.pdf")], [])
    with pytest.raises(ReviewError):
        svc.release(owner, ["R001"])
    with svc.factory() as s:
        assert s.get(ReviewRelease, release).payload == original
    assert svc.queue(a)[0]["tasks"][0]["status"] == "stale"


def test_double_review_blindness_disagreement_and_adjudication(review):
    svc, owner, a, b, c = review
    svc.set_policy(
        owner, {**DEFAULT_POLICY, "review_count": 2, "sample_percent": 0, "high_risk_flags": False}
    )
    first = svc.assign(owner, "R001", a)
    second = svc.assign(owner, "R001", b)
    completed(svc, a, first)
    assert svc.detail(b, "R001")["submissions"] == []
    assert svc.detail(b, "R001")["content"]["notes"] == ""
    with pytest.raises(ReviewError):
        svc.approve(owner, "R001")
    detail = svc.detail(b, "R001")
    edited = detail["content"]
    edited["notes"] = "Second reviewer notes"
    svc.save(b, second, detail["task"]["revision"], edited)
    completed(svc, b, second)
    with pytest.raises(ReviewError, match="disagree"):
        svc.approve(owner, "R001")
    subs = svc.detail(owner, "R001")["submissions"]
    svc.adjudicate(owner, "R001", subs[0]["content"], "Compared both reviews with the original scan.")
    assert svc.approve(owner, "R001")
    with svc.factory() as s:
        assert len(list(s.scalars(select(ReviewSubmission)))) == 2


def test_stale_browser_save_reassignment_and_input_change_rejected(review, tmp_path):
    svc, owner, a, b, c = review
    task = svc.assign(owner, "R001", a)
    detail = svc.detail(a, "R001")
    data = detail["content"]
    data["notes"] = "Saved edit"
    saved = svc.save(a, task, detail["task"]["revision"], data)
    with pytest.raises(ReviewError, match="newer draft"):
        svc.save(a, task, detail["task"]["revision"], data)
    svc.cancel(owner, task, "Reassigned to another reviewer")
    with pytest.raises(ReviewError):
        svc.submit(a, task, saved["revision"])
    new = svc.assign(owner, "R001", b)
    ingest_batch(svc.factory, svc.workspace.store, svc.project_id, [pdf(tmp_path / "R001-A1.pdf")], [])
    with pytest.raises(ReviewError):
        svc.save(b, new, 1, data)


def test_unassigned_member_cannot_read_or_edit_and_reviewer_cannot_approve(review):
    svc, owner, a, b, c = review
    task = svc.assign(owner, "R001", a)
    with pytest.raises(ReviewError):
        svc.detail(b, "R001")
    with pytest.raises(ReviewError):
        svc.save(b, task, 1, svc.detail(a, "R001")["content"])
    with pytest.raises(ReviewError):
        svc.assign(a, "R001", b)
    completed(svc, a, task)
    with pytest.raises(ReviewError):
        svc.approve(a, "R001")


def test_help_is_not_independent_review_and_sessions_revoke(review):
    svc, owner, a, b, c = review
    task = svc.assign(owner, "R001", a)
    help_id = svc.request_help(a, task, "Is this amount one or two?")
    svc.assign_help(owner, help_id, c)
    assert svc.detail(c, "R001")["content"]
    svc.help_reply(c, help_id, "The scan appears to say two.")
    with pytest.raises(ReviewError):
        svc.assign(owner, "R001", c)
    token, _ = svc.login("casey@example.org", "review-password-123")
    assert svc.authenticate(token)["id"] == c
    svc.set_member_active(owner, c, False)
    with pytest.raises(ReviewError):
        svc.authenticate(token)


def test_page_gate_and_structural_reference_cannot_be_waived(review):
    svc, owner, a, b, c = review
    task = svc.assign(owner, "R001", a)
    with pytest.raises(ReviewError, match="every source page"):
        svc.submit(a, task, 1)
    detail = svc.detail(a, "R001")
    data = detail["content"]
    data["items"][0]["evidence"][0]["source_record_id"] = "unrelated"
    for page in data["pages"]:
        page["reviewed"] = True
    saved = svc.save(a, task, 1, data)
    for issue in saved["content"]["issues"]:
        issue["resolution"] = "Waive this issue"
    saved = svc.save(a, task, saved["revision"], saved["content"])
    svc.submit(a, task, saved["revision"])
    with pytest.raises(ReviewError, match="structural"):
        svc.approve(owner, "R001")


def test_observer_cannot_become_independent_reviewer(review):
    svc, owner, a, b, c = review
    task = svc.assign(owner, "R001", a)
    completed(svc, a, task)
    assert svc.detail(owner, "R001")["submissions"]
    with pytest.raises(ReviewError, match="already seen"):
        svc.assign(owner, "R001", owner)


def test_new_ai_candidate_invalidates_old_reviews(review):
    svc, owner, a, b, c = review
    task = svc.assign(owner, "R001", a)
    completed(svc, a, task)
    svc.approve(owner, "R001")
    r = svc.workspace.records()[0]
    with svc.factory.begin() as s:
        s.add(
            Candidate(
                record_pk=r.id,
                input_version=r.input_version,
                content=RecordContent.model_validate(sample()).model_dump(mode="json"),
                provenance={"rerun": True},
            )
        )
    with pytest.raises(ReviewError):
        svc.release(owner, ["R001"])
    assert svc.queue(a)[0]["tasks"][0]["status"] == "stale"


def test_returning_an_approved_submission_revokes_current_approval(review):
    svc, owner, a, b, c = review
    task = svc.assign(owner, "R001", a)
    completed(svc, a, task)
    svc.approve(owner, "R001")
    release = svc.release(owner, ["R001"])
    svc.cancel(owner, task, "Recheck the recorded portion against the scan")
    with pytest.raises(ReviewError):
        svc.release(owner, ["R001"])
    assert svc.detail(owner, "R001")["approved_id"] is None
    with svc.factory() as s:
        assert s.get(ReviewRelease, release).payload["records"]


def test_incomplete_issue_clears_when_flag_is_cleared_and_does_not_attribute_actor(review):
    svc, owner, a, b, c = review
    task = svc.assign(owner, "R001", a)
    d = svc.detail(a, "R001")
    content = d["content"]
    content["pages"][0]["source_incomplete"] = True
    saved = svc.save(a, task, d["task"]["revision"], content)
    warning = next(x for x in saved["content"]["issues"] if x["code"] == "source_incomplete")
    assert "reviewer" not in warning["message"].lower()
    content = saved["content"]
    content["pages"][0]["source_incomplete"] = False
    saved = svc.save(a, task, saved["revision"], content)
    assert not any(x["code"] == "source_incomplete" for x in saved["content"]["issues"])
    # Old saved drafts are normalized for display, without rewriting immutable history.
    with svc.factory.begin() as s:
        t = s.get(ReviewTask, task)
        raw = copy.deepcopy(t.draft)
        raw["issues"].append(warning)
        t.draft = raw
    assert not any(x["code"] == "source_incomplete" for x in svc.detail(a, "R001")["content"]["issues"])
    with svc.factory() as s:
        assert any(x["code"] == "source_incomplete" for x in s.get(ReviewTask, task).draft["issues"])


def test_issue_notes_optional_but_structural_errors_cannot_be_waived(review):
    svc, owner, a, b, c = review
    task = svc.assign(owner, "R001", a)
    d = svc.detail(a, "R001")
    data = d["content"]
    data["issues"].append(
        {
            "code": "UNCLEAR_FOOD_NAME",
            "target": "i1",
            "severity": "blocking",
            "message": "Compare this transcription with the scan.",
            "resolution": None,
        }
    )
    svc.save(a, task, d["task"]["revision"], data)
    completed(svc, a, task)
    assert svc.approve(owner, "R001")
    d = svc.detail(owner, "R001")
    assert next(i for i in d["content"]["issues"] if i["code"] == "UNCLEAR_FOOD_NAME")["resolution"] is None
    data = copy.deepcopy(d["content"])
    data["items"][0]["evidence"][0]["source_record_id"] = "MISSING"
    with svc.factory() as s:
        r = svc.record(s, "R001")
        with pytest.raises(ReviewError, match="structural"):
            svc.checked(data, r, svc.snapshot(s, r), svc.latest(s, r).content, final=True)
