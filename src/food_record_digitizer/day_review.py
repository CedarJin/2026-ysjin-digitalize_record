"""Review complete participant/date units while retaining source-level provenance."""

from __future__ import annotations

import copy
from functools import wraps
from threading import RLock
from types import SimpleNamespace

from sqlalchemy import select

from . import db
from .day_grouping import iso_date, subject, sync_day_units
from .ingest import current_snapshot
from .review import ReviewError, ReviewService


class DayReviewService(ReviewService):
    Record = db.DayReviewUnit
    ReviewTask = db.DayReviewTask
    ReviewEdit = db.DayReviewEdit
    ReviewSubmission = db.DayReviewSubmission
    ReviewHelp = db.DayReviewHelp
    ReviewDecision = db.DayReviewDecision
    ReviewApproval = db.DayReviewApproval

    def __init__(self, workspace):
        super().__init__(workspace)
        self._sync_lock = RLock()

    def latest(self, s, record):
        self.snapshot(s, record)
        return SimpleNamespace(
            id=record.fingerprint,
            content=record.content,
            provenance={"review_unit": "participant_date", "members": record.snapshot["members"]},
        )

    def snapshot(self, s, record):
        # Validate source versions again inside the operation's transaction.
        for member in record.snapshot["members"]:
            source = s.scalar(select(db.Record).where(db.Record.id == member["record_pk"]).with_for_update())
            candidate = ReviewService.latest(self, s, source)
            if (
                source.input_version != member["input_version"]
                or (candidate.id if candidate else None) != member["candidate_id"]
                or source.active_run_id != member["active_run_id"]
                or source.state == "excluded"
            ):
                raise ReviewError("Source membership changed. Reload the day before continuing.", 409)
        return record.snapshot

    def record(self, s, record_id):
        try:
            record = super().record(s, record_id)
        except ReviewError:
            raise ReviewError(
                "Review now uses participant/date groups. Open the day queue; previous record reviews remain in the archive.",
                409,
            ) from None
        if record.state == "retired":
            raise ReviewError("This day has been regrouped. Reload the day queue.", 409)
        self.snapshot(s, record)
        return record

    def fresh(self, s, task, record):
        return record.state != "retired" and super().fresh(s, task, record)

    def editable(self, s, actor, task_id, revision):
        if not s.get(self.ReviewTask, task_id):
            raise ReviewError(
                "This record-level task has been superseded by day review. Your saved work is preserved in the archive. Download any unsaved edits before opening the day queue.",
                409,
            )
        return super().editable(s, actor, task_id, revision)

    def assign(self, actor_id, record_id, assignee_id, due_at=None):
        with self.factory() as s:
            self.actor(s, actor_id, True)
            unit = self.record(s, record_id)
            for member in unit.snapshot["members"]:
                for exposure in s.scalars(
                    select(db.Audit).where(
                        db.Audit.project_id == self.project_id,
                        db.Audit.actor_id == assignee_id,
                        db.Audit.action == "review_answers_viewed",
                        db.Audit.target_id == member["record_pk"],
                    )
                ):
                    if exposure.detail.get("input_version") == member["input_version"]:
                        raise ReviewError(
                            "This member has seen another review of a source in this day and cannot independently review it.",
                            409,
                        )
        return super().assign(actor_id, record_id, assignee_id, due_at)

    def checked(self, content, record, snapshot, baseline, final=False):
        days = content.get("days", [])
        if not days:
            raise ReviewError("At least one intake day is required; an unknown date may be blank.")
        sid = content.get("subject", {}).get("normalized")
        if sid is not None and sid != record.subject_id:
            raise ReviewError("Participant must match this review. Ask the owner to correct metadata.")
        if final and sid != record.subject_id:
            raise ReviewError("Confirm the participant before approval.")
        # Review assignments remain stable, including offline packages. Per-food
        # date corrections follow event.day_id and are exported as effective dates.
        return super().checked(content, record, snapshot, baseline, final)

    def queue(self, actor_id):
        rows = super().queue(actor_id)
        with self.factory() as s:
            result = []
            for row in rows:
                unit = s.get(db.DayReviewUnit, row["record_id"])
                if unit.state == "retired":
                    continue
                row.update(self.labels(unit))
                row["has_candidate"] = any(m["candidate_id"] for m in unit.snapshot["members"])
                result.append(row)
            return sorted(result, key=lambda r: (r["subject_id"], r["date"] or ""))

    @staticmethod
    def labels(unit):
        return {
            "review_unit_type": "participant_date",
            "subject_id": unit.subject_id,
            "date": iso_date(unit.date),
            "source_record_ids": sorted({m["record_id"] for m in unit.snapshot["members"]}),
        }

    def detail(self, actor_id, record_id):
        result = super().detail(actor_id, record_id)
        with self.factory() as s:
            result.update(self.labels(s.get(db.DayReviewUnit, record_id)))
        return result

    def release_metadata(self, s, payload):
        payload["review_unit_type"] = "participant_date"
        for row in payload["records"]:
            row.update(self.labels(s.get(db.DayReviewUnit, row["record_id"])))
        payload["source_processing_report"] = [
            {
                "record_id": r.record_id,
                "input_version": r.input_version,
                "state": r.state,
                "day_unit_ids": [
                    u.id
                    for u in s.scalars(
                        select(db.DayReviewUnit).where(
                            db.DayReviewUnit.project_id == self.project_id,
                            db.DayReviewUnit.state != "retired",
                        )
                    )
                    if any(m["record_pk"] == r.id for m in u.snapshot["members"])
                ],
            }
            for r in s.scalars(select(db.Record).where(db.Record.project_id == self.project_id))
        ]
        payload["unresolved_source_days"] = [
            {"placement_id": p.id, "record_id": r.record_id, "day_id": p.day_id, "proposed": p.proposed}
            for p, r in s.execute(
                select(db.DayPlacement, db.Record)
                .join(db.Record, db.Record.id == db.DayPlacement.record_pk)
                .where(
                    db.DayPlacement.project_id == self.project_id,
                    db.DayPlacement.status == "needs_confirmation",
                )
            )
            if p.input_version == r.input_version
            and p.candidate_key == ((c.id if c else "") if (c := ReviewService.latest(self, s, r)) else "")
        ]
        return payload

    def release(self, actor_id, record_ids):
        # The base release freezes approval content and every membership/source revision.
        for rid in record_ids:
            with self.factory() as s:
                self.record(s, rid)
        return super().release(actor_id, record_ids)

    def placements(self, actor_id):
        with self.factory() as s:
            self.actor(s, actor_id, True)
            result = []
            for r in s.scalars(
                select(db.Record).where(
                    db.Record.project_id == self.project_id, db.Record.state != "excluded"
                )
            ):
                if not current_snapshot(s, r)["documents"]:
                    continue
                c = ReviewService.latest(self, s, r)
                for p in s.scalars(
                    select(db.DayPlacement).where(
                        db.DayPlacement.record_pk == r.id,
                        db.DayPlacement.input_version == r.input_version,
                        db.DayPlacement.candidate_key == (c.id if c else ""),
                    )
                ):
                    result.append(
                        {
                            "id": p.id,
                            "record_id": r.record_id,
                            "day_id": p.day_id,
                            "revision": p.revision,
                            "subject_id": p.subject_id,
                            "date": p.date,
                            "proposed": p.proposed,
                            "status": p.status,
                            "reason": p.reason,
                            "has_candidate": bool(c),
                            "documents": current_snapshot(s, r)["documents"],
                        }
                    )
            return sorted(
                result, key=lambda p: (p["status"] != "needs_confirmation", p["record_id"], p["day_id"])
            )

    def resolve_placement(self, actor_id, placement_id, revision, subject_id, date, reason):
        sid, day_date = subject(subject_id), iso_date(date)
        if not sid or (date and not day_date) or not reason.strip():
            raise ReviewError("A subject and grouping reason are required. Leave an unknown date blank.")
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            p = s.get(db.DayPlacement, placement_id)
            if not p or p.project_id != self.project_id:
                raise ReviewError("Source day not found.", 404)
            r = s.get(db.Record, p.record_pk)
            c = ReviewService.latest(self, s, r)
            if (
                p.revision != revision
                or p.input_version != r.input_version
                or p.candidate_key != (c.id if c else "")
            ):
                raise ReviewError("Source day changed. Reload before confirming its grouping.", 409)
            if not p.day_id.startswith("manual:"):
                meta = current_snapshot(s, r)["metadata"].get(r.record_id, {})
                if subject(meta.get("subject_id")) and sid != subject(meta.get("subject_id")):
                    raise ReviewError("Participant must follow metadata. Correct metadata first.")
            before = {"subject_id": p.subject_id, "date": p.date}
            p.subject_id, p.date, p.reason = sid, day_date, reason.strip()
            p.status, p.confirmed_by = "owner_confirmed", actor.id
            s.flush()
            self.audit(
                s,
                actor,
                "day_grouping_confirmed",
                p.id,
                {
                    "before": before,
                    "subject_id": sid,
                    "date": day_date,
                    "reason": reason,
                    "revision": p.revision,
                },
            )
        sync_day_units(self)
        return {"ok": True}

    def add_source_day(self, actor_id, placement_id, revision, subject_id, date, reason):
        sid, day_date = subject(subject_id), iso_date(date)
        if not sid or not day_date or not reason.strip():
            raise ReviewError("A subject, ISO date and source-based reason are required.")
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            p = s.get(db.DayPlacement, placement_id)
            if not p or p.project_id != self.project_id:
                raise ReviewError("Source day not found.", 404)
            r = s.get(db.Record, p.record_pk)
            c = ReviewService.latest(self, s, r)
            if (
                p.revision != revision
                or p.input_version != r.input_version
                or p.candidate_key != (c.id if c else "")
            ):
                raise ReviewError("Source changed. Reload its grouping.", 409)
            existing = list(
                s.scalars(
                    select(db.DayPlacement).where(
                        db.DayPlacement.record_pk == r.id,
                        db.DayPlacement.input_version == r.input_version,
                        db.DayPlacement.candidate_key == p.candidate_key,
                    )
                )
            )
            if any(x.subject_id == sid and x.date == day_date for x in existing):
                raise ReviewError("This source already has a placement for that participant and date.", 409)
            new = db.DayPlacement(
                project_id=self.project_id,
                record_pk=r.id,
                input_version=r.input_version,
                candidate_key=p.candidate_key,
                day_id="manual:" + db.uid(),
                subject_id=sid,
                date=day_date,
                proposed=p.proposed,
                status="owner_confirmed",
                confirmed_by=actor.id,
                reason=reason.strip(),
            )
            s.add(new)
            s.flush()
            self.audit(
                s,
                actor,
                "additional_source_day",
                new.id,
                {"record_id": r.record_id, "subject_id": sid, "date": day_date, "reason": reason},
            )
        sync_day_units(self)
        return {"ok": True}

    def placement_snapshot(self, actor_id, placement_id):
        with self.factory() as s:
            self.actor(s, actor_id, True)
            p = s.get(db.DayPlacement, placement_id)
            if not p or p.project_id != self.project_id:
                raise ReviewError("Source day not found.", 404)
            return current_snapshot(s, s.get(db.Record, p.record_pk))

    def hide_archive(self, actor_id, task_id):
        """Hide an own legacy draft from the list without deleting any review data."""
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id)
            task = s.get(db.ReviewTask, task_id)
            record = s.get(db.Record, task.record_pk) if task else None
            if not record or record.project_id != self.project_id or task.assignee_id != actor.id:
                raise ReviewError("Previous review not found.", 404)
            hidden = s.scalar(
                select(db.Audit).where(
                    db.Audit.project_id == self.project_id,
                    db.Audit.actor_id == actor.id,
                    db.Audit.action == "archive_review_hidden",
                    db.Audit.target_id == task.id,
                )
            )
            if not hidden:
                self.audit(s, actor, "archive_review_hidden", task.id, {"record_id": record.record_id})
            return {"hidden": True}

    def archive(self, actor_id):
        """Own saved record drafts only: never expose an independent reviewer's answers."""
        with self.factory() as s:
            actor = self.actor(s, actor_id)
            hidden = set(
                s.scalars(
                    select(db.Audit.target_id).where(
                        db.Audit.project_id == self.project_id,
                        db.Audit.actor_id == actor.id,
                        db.Audit.action == "archive_review_hidden",
                    )
                )
            )
            return [
                {
                    "record_id": r.record_id,
                    "task_id": t.id,
                    "input_version": t.input_version,
                    "status": t.status,
                    "content": copy.deepcopy(t.draft),
                    "created_at": t.created_at,
                }
                for t, r in s.execute(
                    select(db.ReviewTask, db.Record)
                    .join(db.Record, db.Record.id == db.ReviewTask.record_pk)
                    .where(db.Record.project_id == self.project_id, db.ReviewTask.assignee_id == actor.id)
                )
                if t.id not in hidden
            ]


def reconciled(method):
    """Authenticate before reconciliation; serialize threads on SQLite, with DB version CAS across workers."""

    @wraps(method)
    def call(self, actor_id, *args, **kwargs):
        with self.factory() as s:
            self.actor(s, actor_id)
        with self._sync_lock:
            sync_day_units(self)
            return method(self, actor_id, *args, **kwargs)

    return call


for _name in (
    "queue",
    "detail",
    "assign",
    "save",
    "submit",
    "cancel",
    "adjudicate",
    "approve",
    "release",
    "request_help",
    "assign_help",
    "help_reply",
    "exclude",
    "restore",
    "placements",
    "resolve_placement",
    "add_source_day",
):
    setattr(DayReviewService, _name, reconciled(getattr(DayReviewService, _name)))
