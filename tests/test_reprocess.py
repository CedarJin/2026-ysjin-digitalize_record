# ruff: noqa: F811
import copy

import pytest
from sqlalchemy import select
from test_day_review import day_review, finish  # noqa: F401
from test_desktop import wait_queue
from test_extraction import FakeProvider
from test_ingest import ws  # noqa: F401

from food_record_digitizer import db
from food_record_digitizer.desktop_queue import DEFAULTS, DesktopQueue
from food_record_digitizer.ingest import InputError


def test_explicit_reprocess_bypasses_reuse_retains_history_and_invalidates_review(day_review):
    svc, owner, reviewer, _, _ = day_review
    rid = svc.queue(owner)[0]["record_id"]
    task = svc.assign(owner, rid, reviewer)
    finish(svc, reviewer, task)
    svc.approve(owner, rid)
    release_id = svc.release(owner, [rid])
    with svc.factory() as session:
        old = session.scalar(select(db.Candidate))
        old_id, old_content = old.id, copy.deepcopy(old.content)
        release_payload = copy.deepcopy(session.get(db.ReviewRelease, release_id).payload)
    provider = FakeProvider()
    q = DesktopQueue(svc.workspace, lambda config: provider)
    try:
        normal = q.preview(["R001"])
        assert normal["reuse"] == 1 and normal["requests"] == 0
        scope = q.preview(["R001"], force=True)
        assert scope["reuse"] == 0 and scope["requests"] == 1 and scope["reprocess"] == 1
        with pytest.raises(InputError, match="Preview re-processing"):
            q.enqueue(["R001"], DEFAULTS, scope["versions"], force=True)
        with q.lock:
            q.enqueue(
                ["R001"],
                DEFAULTS,
                scope["versions"],
                force=True,
                processing_tokens=scope["processing_tokens"],
            )
            assert q.records()[0]["state"] == "queued"
            with pytest.raises(InputError, match="Preview re-processing"):
                q.enqueue(
                    ["R001"],
                    DEFAULTS,
                    scope["versions"],
                    force=True,
                    processing_tokens=scope["processing_tokens"],
                )
        wait_queue(q)
        assert provider.calls == 1
        # Same sources and configuration still require a NEW provider call when forced.
        scope = q.preview(["R001"], force=True)
        q.enqueue(
            ["R001"], DEFAULTS, scope["versions"], force=True, processing_tokens=scope["processing_tokens"]
        )
        wait_queue(q)
        assert provider.calls == 2
        assert not svc.detail(owner, rid)["approved_id"]
        with svc.factory() as session:
            assert session.get(db.Candidate, old_id).content == old_content
            assert len(list(session.scalars(select(db.Candidate)))) == 3
            assert session.get(db.DayReviewTask, task).status == "stale"
            assert session.get(db.ReviewRelease, release_id).payload == release_payload
    finally:
        q.close()


def test_failed_reprocess_keeps_approved_data_and_requires_unknown_retry(day_review, monkeypatch):
    svc, owner, reviewer, _, _ = day_review
    rid = svc.queue(owner)[0]["record_id"]
    task = svc.assign(owner, rid, reviewer)
    finish(svc, reviewer, task)
    svc.approve(owner, rid)
    before = svc.detail(owner, rid)["approved_id"]
    monkeypatch.setattr(
        "food_record_digitizer.desktop_queue.extract_record",
        lambda *a, **k: {"status": "failed", "error": "request_timeout_outcome_unknown"},
    )
    q = DesktopQueue(svc.workspace, lambda config: FakeProvider())
    try:
        scope = q.preview(["R001"], force=True)
        q.enqueue(
            ["R001"], DEFAULTS, scope["versions"], force=True, processing_tokens=scope["processing_tokens"]
        )
        wait_queue(q)
        assert q.records()[0]["state"] == "needs_attention"
        assert q.records()[0]["has_ai_candidate"]
        scope = q.preview(["R001"], force=True)
        assert scope["unknown"] == ["R001"]
        with pytest.raises(InputError, match="charged"):
            q.enqueue(
                ["R001"],
                DEFAULTS,
                scope["versions"],
                force=True,
                processing_tokens=scope["processing_tokens"],
            )
        assert svc.detail(owner, rid)["approved_id"] == before
    finally:
        q.close()


def test_processing_scope_survives_app_restart(day_review):
    svc, *_ = day_review
    q = DesktopQueue(svc.workspace, lambda config: FakeProvider())
    preview = q.preview(["R001"])
    batch = q.enqueue(["R001"], DEFAULTS, preview["versions"])
    q.close()
    reopened = DesktopQueue(svc.workspace, lambda config: FakeProvider())
    try:
        history = reopened.batch_history()
        assert history[0]["id"] == batch["batch_id"]
        assert history[0]["record_ids"] == ["R001"]
        assert reopened.status()["active"] == 0
    finally:
        reopened.close()
