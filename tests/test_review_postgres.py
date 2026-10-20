"""Run with FOOD_RECORD_TEST_POSTGRES_URL against a disposable PostgreSQL database."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy.orm.exc import StaleDataError
from test_review import completed, review

from food_record_digitizer.review import DEFAULT_POLICY, ReviewError
from food_record_digitizer.workspace import Workspace


def test_postgres_independent_review_and_competing_saves(tmp_path, monkeypatch):
    url = os.environ.get("FOOD_RECORD_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set FOOD_RECORD_TEST_POSTGRES_URL for PostgreSQL acceptance")
    monkeypatch.setenv("FOOD_RECORD_DATABASE_URL", url)
    workspace = Workspace.initialize(tmp_path / "postgres", "pg-" + uuid4().hex[:8], "PostgreSQL acceptance")
    svc, owner, a, b, _ = review.__wrapped__(workspace, tmp_path)
    svc.set_policy(
        owner, {**DEFAULT_POLICY, "review_count": 2, "high_risk_flags": False, "sample_percent": 0}
    )
    ta, tb = svc.assign(owner, "R001", a), svc.assign(owner, "R001", b)
    data = svc.detail(a, "R001")["content"]
    barrier = Barrier(2)

    def save(note):
        import copy

        draft = copy.deepcopy(data)
        draft["notes"] = note
        barrier.wait()
        try:
            svc.save(a, ta, 1, draft)
            return "saved"
        except (ReviewError, StaleDataError):
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["first tab", "second tab"]))
    assert sorted(results) == ["conflict", "saved"]
    assert svc.detail(b, "R001")["content"]["notes"] == ""
    completed(svc, a, ta)
    completed(svc, b, tb)
    with pytest.raises(ReviewError):
        svc.approve(owner, "R001")
    svc.adjudicate(
        owner,
        "R001",
        svc.detail(owner, "R001")["submissions"][0]["content"],
        "Compared independent reviews with source",
    )
    assert svc.approve(owner, "R001")
    assert svc.release(owner, ["R001"])


def test_postgres_day_review_across_workers(tmp_path, monkeypatch):
    import copy

    from test_day_review import day_review, finish

    from food_record_digitizer.day_review import DayReviewService

    url = os.environ.get("FOOD_RECORD_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set FOOD_RECORD_TEST_POSTGRES_URL for PostgreSQL acceptance")
    monkeypatch.setenv("FOOD_RECORD_DATABASE_URL", url)
    workspace = Workspace.initialize(tmp_path / "pg-day", "pg-" + uuid4().hex[:8], "Day review acceptance")
    svc, owner, a, b, _ = day_review.__wrapped__(workspace, tmp_path)
    svc.set_policy(
        owner, {**DEFAULT_POLICY, "review_count": 2, "high_risk_flags": False, "sample_percent": 0}
    )
    rid = svc.queue(owner)[0]["record_id"]
    ta, tb = svc.assign(owner, rid, a), svc.assign(owner, rid, b)
    data = svc.detail(a, rid)["content"]
    barrier = Barrier(2)

    def save(note):
        worker = DayReviewService(workspace)
        draft = copy.deepcopy(data)
        draft["notes"] = note
        barrier.wait()
        try:
            worker.save(a, ta, 1, draft)
            return "saved"
        except (ReviewError, StaleDataError):
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ["first", "second"])) == ["conflict", "saved"]
    assert svc.detail(b, rid)["content"]["notes"] == ""
    finish(svc, a, ta)
    finish(svc, b, tb)
    svc.adjudicate(owner, rid, svc.detail(owner, rid)["submissions"][0]["content"], "Synthetic comparison")
    assert svc.approve(owner, rid)
    assert svc.release(owner, [rid])
