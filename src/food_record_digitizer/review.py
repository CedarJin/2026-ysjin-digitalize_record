"""Transactional review workflow. No model access or provider keys are needed."""

from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from .db import (
    Audit,
    Candidate,
    Record,
    ReviewApproval,
    ReviewDecision,
    ReviewEdit,
    ReviewHelp,
    ReviewPolicy,
    ReviewRelease,
    ReviewSession,
    ReviewSubmission,
    ReviewTask,
    ReviewUser,
    now,
)
from .ingest import current_snapshot, digest
from .schemas import PageCoverage, RecordContent
from .validation import APPROVAL_BLOCKING_CODES, refresh_issues, validate_content


class ReviewError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ReviewError("Use a password of 12–256 characters.")
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ":" + value


def password_matches(password, encoded):
    if not encoded or len(password) > 256:
        return False
    salt, expected = encoded.split(":")
    actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(actual, expected)


def future(hours=12):
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


DEFAULT_POLICY = {
    "review_count": 1,
    "high_risk_review_count": 2,
    "sample_percent": 10,
    "sample_seed": "review-v1",
    "high_risk_flags": True,
}


class ReviewService:
    Record = Record
    ReviewApproval = ReviewApproval
    ReviewDecision = ReviewDecision
    ReviewEdit = ReviewEdit
    ReviewHelp = ReviewHelp
    ReviewSubmission = ReviewSubmission
    ReviewTask = ReviewTask

    def snapshot(self, s, record):
        return current_snapshot(s, record)

    def __init__(self, workspace):
        self.workspace = workspace
        self.factory = workspace.factory
        self.project_id = workspace.project_id

    def audit(self, s, actor, action, target, detail=None):
        s.add(
            Audit(
                project_id=self.project_id,
                actor_id=actor.id,
                action=action,
                target_id=target,
                detail=detail or {},
            )
        )

    def actor(self, s, user_id, owner=False):
        user = s.get(ReviewUser, user_id)
        if not user or user.project_id != self.project_id or not user.active:
            raise ReviewError("Sign in again.", 401)
        if owner and user.role != "owner":
            raise ReviewError("Owner access required.", 403)
        return user

    def policy(self, s):
        return s.scalar(
            select(ReviewPolicy)
            .where(ReviewPolicy.project_id == self.project_id)
            .order_by(ReviewPolicy.created_at.desc(), ReviewPolicy.id.desc())
        )

    def latest(self, s, record):
        return s.scalar(
            select(Candidate)
            .where(Candidate.record_pk == record.id, Candidate.input_version == record.input_version)
            .order_by(Candidate.created_at.desc(), Candidate.id.desc())
        )

    def record(self, s, record_id):
        r = s.scalar(
            select(self.Record)
            .where(self.Record.project_id == self.project_id, self.Record.record_id == record_id)
            .with_for_update()
        )
        if not r:
            raise ReviewError("Record not found.", 404)
        return r

    def fresh(self, s, task, record):
        latest = self.latest(s, record)
        return (
            task.input_version == record.input_version
            and latest is not None
            and task.candidate_id == latest.id
            and task.policy_id == self.policy(s).id
        )

    def access(self, s, user, record):
        if user.role == "owner":
            return
        tasks = list(s.scalars(select(self.ReviewTask).where(self.ReviewTask.record_pk == record.id)))
        if any(
            t.assignee_id == user.id
            and t.status not in {"cancelled", "stale", "returned"}
            and self.fresh(s, t, record)
            for t in tasks
        ):
            return
        for help_item in s.scalars(
            select(self.ReviewHelp).where(
                self.ReviewHelp.helper_id == user.id, self.ReviewHelp.status != "closed"
            )
        ):
            t = s.get(self.ReviewTask, help_item.task_id)
            if (
                t.record_pk == record.id
                and t.status not in {"cancelled", "stale", "returned"}
                and self.fresh(s, t, record)
            ):
                return
        raise ReviewError("This record is not assigned to you.", 403)

    def required(self, record, candidate, policy, snapshot):
        cfg = policy.settings
        high = cfg["high_risk_flags"] and (
            any(m.get("flag") for m in snapshot["metadata"].values())
            or any(i.get("severity") == "blocking" for i in candidate.content.get("issues", []))
        )
        sampled = int(digest([cfg["sample_seed"], record.record_id])[:8], 16) % 100 < cfg["sample_percent"]
        return max(
            record.required_reviews,
            cfg["review_count"],
            cfg["high_risk_review_count"] if high or sampled else 1,
        )

    def bootstrap(self, email, name, password):
        encoded = password_hash(password)
        with self.factory.begin() as s:
            if s.scalar(select(ReviewUser).where(ReviewUser.project_id == self.project_id)):
                raise ReviewError("An owner already exists; use invitations to add members.")
            user = ReviewUser(
                project_id=self.project_id,
                email=email.strip().lower(),
                name=name.strip(),
                role="owner",
                password_hash=encoded,
            )
            s.add(user)
            s.flush()
            s.add(ReviewPolicy(project_id=self.project_id, settings=DEFAULT_POLICY, created_by=user.id))
            self.audit(s, user, "owner_created", user.id)
            return user.id

    def bootstrap_invite(self, email, name):
        """Initialize the first owner without a shared or default password."""
        if "@" not in email or not name.strip():
            raise ReviewError("Provide an owner name and email.")
        with self.factory.begin() as session:
            if session.scalar(select(ReviewUser).where(ReviewUser.project_id == self.project_id)):
                raise ReviewError("An owner already exists; use the owner's invitation controls.")
            token = secrets.token_urlsafe(32)
            user = ReviewUser(
                project_id=self.project_id,
                email=email.strip().lower(),
                name=name.strip(),
                role="owner",
                invite_hash=token_hash(token),
                invite_expires=future(24),
            )
            session.add(user)
            session.flush()
            session.add(ReviewPolicy(project_id=self.project_id, settings=DEFAULT_POLICY, created_by=user.id))
            self.audit(session, user, "owner_invited", user.id)
            return {"user_id": user.id, "token": token, "email": user.email}

    def renew_invite(self, actor_id, user_id):
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            user = s.get(ReviewUser, user_id)
            if not user or user.project_id != self.project_id or user.id == actor.id:
                raise ReviewError("Select another project member.")
            token = secrets.token_urlsafe(32)
            user.active = 1
            user.password_hash = None
            user.invite_hash = token_hash(token)
            user.invite_expires = future(24)
            for item in s.scalars(select(ReviewSession).where(ReviewSession.user_id == user_id)):
                s.delete(item)
            self.audit(s, actor, "invitation_renewed", user.id)
            return {"token": token, "user_id": user.id, "expires_at": user.invite_expires}

    def restore(self, actor_id, record_id, reason):
        if not reason.strip():
            raise ReviewError("A restoration reason is required.")
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            record = self.record(s, record_id)
            if record.state != "excluded":
                raise ReviewError("Only excluded records can be restored.")
            record.state = "pending_review" if self.latest(s, record) else "pending_extraction"
            record.approved_id = None
            for task in s.scalars(select(self.ReviewTask).where(self.ReviewTask.record_pk == record.id)):
                task.status = "stale"
            self.audit(s, actor, "record_restored", record.id, {"reason": reason})

    def login(self, email, password):
        with self.factory.begin() as s:
            user = s.scalar(
                select(ReviewUser).where(
                    ReviewUser.project_id == self.project_id, ReviewUser.email == email.strip().lower()
                )
            )
            if not user or not user.active or not password_matches(password, user.password_hash):
                raise ReviewError("Email or password is incorrect.", 401)
            token = secrets.token_urlsafe(32)
            session = ReviewSession(
                id=token_hash(token), user_id=user.id, csrf=secrets.token_urlsafe(32), expires_at=future()
            )
            s.add(session)
            self.audit(s, user, "signed_in", user.id)
            return token, session.csrf

    def authenticate(self, token):
        with self.factory() as s:
            session = s.get(ReviewSession, token_hash(token or ""))
            if not session or session.expires_at <= now():
                raise ReviewError("Your session expired. Sign in again.", 401)
            user = self.actor(s, session.user_id)
            return {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "role": user.role,
                "csrf": session.csrf,
                "project_id": self.project_id,
            }

    def logout(self, token):
        with self.factory.begin() as s:
            session = s.get(ReviewSession, token_hash(token or ""))
            if session:
                s.delete(session)

    def invite(self, actor_id, email, name, role="reviewer"):
        if role not in {"owner", "reviewer"} or not name.strip() or "@" not in email:
            raise ReviewError("Provide a name, email and valid role.")
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            email = email.strip().lower()
            user = s.scalar(
                select(ReviewUser).where(ReviewUser.project_id == self.project_id, ReviewUser.email == email)
            )
            if user:
                raise ReviewError("This email already has a membership.", 409)
            token = secrets.token_urlsafe(32)
            user = ReviewUser(
                project_id=self.project_id,
                email=email,
                name=name.strip(),
                role=role,
                invite_hash=token_hash(token),
                invite_expires=future(24),
            )
            s.add(user)
            s.flush()
            self.audit(s, actor, "member_invited", user.id, {"role": role})
            return {"user_id": user.id, "token": token, "expires_at": user.invite_expires}

    def accept_invite(self, token, password):
        encoded = password_hash(password)
        with self.factory.begin() as s:
            user = s.scalar(
                select(ReviewUser)
                .where(ReviewUser.project_id == self.project_id, ReviewUser.invite_hash == token_hash(token))
                .with_for_update()
            )
            if not user or not user.active or user.invite_expires <= now() or user.password_hash:
                raise ReviewError("Invitation is invalid or expired.", 403)
            user.password_hash = encoded
            user.invite_hash = None
            self.audit(s, user, "invitation_accepted", user.id)

    def set_member_active(self, actor_id, user_id, active):
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            user = s.get(ReviewUser, user_id)
            if not user or user.project_id != self.project_id or user.id == actor.id:
                raise ReviewError("Select another project member.")
            user.active = int(bool(active))
            if not active:
                user.invite_hash = None
                for session in s.scalars(select(ReviewSession).where(ReviewSession.user_id == user.id)):
                    s.delete(session)
            self.audit(s, actor, "member_status_changed", user.id, {"active": bool(active)})

    def set_policy(self, actor_id, settings):
        if set(settings) != set(DEFAULT_POLICY):
            raise ReviewError("All review policy fields are required.")
        if (
            settings["review_count"] not in [1, 2]
            or settings["high_risk_review_count"] not in [1, 2]
            or not 0 <= settings["sample_percent"] <= 100
            or not isinstance(settings["sample_seed"], str)
            or not settings["sample_seed"].strip()
            or not isinstance(settings["high_risk_flags"], bool)
        ):
            raise ReviewError("Invalid review policy.")
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            policy = ReviewPolicy(project_id=self.project_id, settings=settings, created_by=actor.id)
            s.add(policy)
            s.flush()
            for record in s.scalars(
                select(self.Record).where(self.Record.project_id == self.project_id).with_for_update()
            ):
                record.approved_id = None
                if record.state in {"approved", "in_review"}:
                    record.state = "pending_review"
                for task in s.scalars(select(self.ReviewTask).where(self.ReviewTask.record_pk == record.id)):
                    if task.status not in {"cancelled", "returned", "stale"}:
                        task.status = "stale"
            self.audit(s, actor, "policy_created", policy.id, settings)
            return policy.id

    def assign(self, actor_id, record_id, assignee_id, due_at=None):
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            assignee = self.actor(s, assignee_id)
            record = self.record(s, record_id)
            if record.state == "excluded":
                raise ReviewError("Restore the record before assigning review.", 409)
            for exposure in s.scalars(
                select(Audit).where(
                    Audit.project_id == self.project_id,
                    Audit.actor_id == assignee.id,
                    Audit.action == "review_answers_viewed",
                    Audit.target_id == record.id,
                )
            ):
                if exposure.detail.get("input_version") == record.input_version:
                    raise ReviewError(
                        "This member has already seen another review and cannot be an independent reviewer.",
                        409,
                    )
            if record.active_run_id:
                raise ReviewError("Wait for extraction to finish before assigning review.", 409)
            candidate = self.latest(s, record)
            snapshot = self.snapshot(s, record)
            if not candidate:
                if record_id not in snapshot["documents"] or any(
                    d["error"] for d in snapshot["documents"].values()
                ):
                    raise ReviewError("A readable main scan is required.")
                content = RecordContent(
                    record_id=record_id,
                    pages=[
                        PageCoverage(
                            source_record_id=sid,
                            page_number=n,
                            classification="food_record" if doc["role"] == "main" else "clarification",
                        )
                        for sid, doc in snapshot["documents"].items()
                        for n in range(1, doc["page_count"] + 1)
                    ],
                )
                candidate = Candidate(
                    record_pk=record.id,
                    input_version=record.input_version,
                    content=content.model_dump(mode="json"),
                    provenance={"method": "manual_entry", "created_by": actor.id},
                )
                s.add(candidate)
                s.flush()
            existing = list(s.scalars(select(self.ReviewTask).where(self.ReviewTask.record_pk == record.id)))
            for task in existing:
                if task.status not in {"cancelled", "returned", "stale"} and not self.fresh(s, task, record):
                    task.status = "stale"
                if task.assignee_id == assignee.id and task.status not in {"cancelled", "stale", "returned"}:
                    raise ReviewError("This reviewer already has a current task.", 409)
            # Discussion participants cannot also count as independent reviewers.
            task_ids = [t.id for t in existing if self.fresh(s, t, record)]
            if task_ids and s.scalar(
                select(self.ReviewHelp).where(
                    self.ReviewHelp.task_id.in_(task_ids), self.ReviewHelp.helper_id == assignee.id
                )
            ):
                raise ReviewError("A discussion helper cannot independently review this input version.", 409)
            task = self.ReviewTask(
                record_pk=record.id,
                input_version=record.input_version,
                candidate_id=candidate.id,
                policy_id=self.policy(s).id,
                assignee_id=assignee.id,
                draft=copy.deepcopy(candidate.content),
                due_at=due_at,
            )
            s.add(task)
            s.flush()
            record.state = "in_review"
            record.approved_id = None
            self.audit(
                s, actor, "task_assigned", task.id, {"assignee_id": assignee.id, "candidate_id": candidate.id}
            )
            return task.id

    def editable(self, s, actor, task_id, revision):
        task = s.get(self.ReviewTask, task_id)
        if not task:
            raise ReviewError("Task not found.", 404)
        record = s.get(self.Record, task.record_pk)
        if record.project_id != self.project_id or task.assignee_id != actor.id:
            raise ReviewError("Only the assigned reviewer can edit this task.", 403)
        if not self.fresh(s, task, record) or task.status in {"stale", "cancelled", "returned", "submitted"}:
            raise ReviewError("This task is no longer editable. Refresh the queue.", 409)
        if task.revision != revision:
            raise ReviewError(
                "A newer draft exists. Reload before saving; your changes were not overwritten.", 409
            )
        return task, record

    def checked(self, content, record, snapshot, baseline, final=False):
        data = RecordContent.model_validate(content)
        if data.record_id != record.record_id:
            raise ReviewError("Record identity cannot be changed.")
        data = refresh_issues(data, snapshot, baseline)
        if final:
            expected = {
                (sid, n) for sid, d in snapshot["documents"].items() for n in range(1, d["page_count"] + 1)
            }
            if {(p.source_record_id, p.page_number) for p in data.pages} != expected or not all(
                p.reviewed and (p.regions or p.classification == "blank") for p in data.pages
            ):
                raise ReviewError("Check every page and record its meaningful regions before approval.")
            if any(
                snapshot["documents"].get(p.source_record_id, {}).get("role") == "attachment"
                and not p.notes.strip()
                for p in data.pages
            ):
                raise ReviewError("Explain the use or non-use of every attachment page before approval.")
            fresh = validate_content(data, snapshot)
            if any(i.code in APPROVAL_BLOCKING_CODES for i in fresh):
                raise ReviewError(
                    "Correct structural references, missing sources, dates and identity before approval."
                )
        return data.model_dump(mode="json")

    def save(self, actor_id, task_id, revision, content, reason="Draft saved"):
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id)
            task, record = self.editable(s, actor, task_id, revision)
            data = self.checked(content, record, self.snapshot(s, record), self.latest(s, record).content)
            task.draft = data
            task.status = "in_progress"
            s.flush()
            s.add(
                self.ReviewEdit(
                    task_id=task.id, actor_id=actor.id, revision=task.revision, content=data, reason=reason
                )
            )
            self.audit(s, actor, "draft_saved", task.id, {"revision": task.revision, "reason": reason})
            return {"revision": task.revision, "content": data}

    def submit(self, actor_id, task_id, revision):
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id)
            task, record = self.editable(s, actor, task_id, revision)
            data = RecordContent.model_validate(task.draft)
            expected = {
                (sid, n)
                for sid, d in self.snapshot(s, record)["documents"].items()
                for n in range(1, d["page_count"] + 1)
            }
            if {(p.source_record_id, p.page_number) for p in data.pages} != expected or not all(
                p.reviewed and (p.regions or p.classification == "blank") for p in data.pages
            ):
                raise ReviewError("Confirm every source page and its meaningful regions before submitting.")
            sub = self.ReviewSubmission(task_id=task.id, reviewer_id=actor.id, content=task.draft)
            s.add(sub)
            s.flush()
            task.status = "submitted"
            self.audit(s, actor, "review_submitted", sub.id, {"task_id": task.id})
            return sub.id

    def cancel(self, actor_id, task_id, reason):
        if not reason.strip():
            raise ReviewError("A return or reassignment reason is required.")
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            task = s.get(self.ReviewTask, task_id)
            if not task or s.get(self.Record, task.record_pk).project_id != self.project_id:
                raise ReviewError("Task not found.", 404)
            record = self.record(s, s.get(self.Record, task.record_pk).record_id)
            if self.fresh(s, task, record):
                record.approved_id = None
                if record.state != "excluded":
                    record.state = "pending_review"
            task.status = "returned" if task.status == "submitted" else "cancelled"
            self.audit(s, actor, "task_returned", task.id, {"reason": reason})

    def current_submissions(self, s, record):
        tasks = list(s.scalars(select(self.ReviewTask).where(self.ReviewTask.record_pk == record.id)))
        ids = [t.id for t in tasks if t.status == "submitted" and self.fresh(s, t, record)]
        return (
            list(s.scalars(select(self.ReviewSubmission).where(self.ReviewSubmission.task_id.in_(ids))))
            if ids
            else []
        )

    def adjudicate(self, actor_id, record_id, content, reason):
        if not reason.strip():
            raise ReviewError("Explain the adjudication decision.")
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            record = self.record(s, record_id)
            candidate = self.latest(s, record)
            subs = self.current_submissions(s, record)
            if not candidate or len(subs) < self.required(
                record, candidate, self.policy(s), self.snapshot(s, record)
            ):
                raise ReviewError("All required independent submissions must arrive first.", 409)
            data = self.checked(content, record, self.snapshot(s, record), candidate.content)
            decision = self.ReviewDecision(
                record_pk=record.id,
                input_version=record.input_version,
                policy_id=self.policy(s).id,
                submission_ids=sorted(x.id for x in subs),
                actor_id=actor.id,
                content=data,
                reason=reason,
            )
            s.add(decision)
            s.flush()
            self.audit(s, actor, "adjudicated", decision.id, {"reason": reason})
            return decision.id

    def approve(self, actor_id, record_id):
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            record = self.record(s, record_id)
            candidate = self.latest(s, record)
            snapshot = self.snapshot(s, record)
            policy = self.policy(s)
            subs = self.current_submissions(s, record)
            if (
                record.state == "excluded"
                or not candidate
                or record.active_run_id
                or len({x.reviewer_id for x in subs}) < self.required(record, candidate, policy, snapshot)
            ):
                raise ReviewError("Required independent reviews are incomplete.", 409)
            active = list(s.scalars(select(self.ReviewTask).where(self.ReviewTask.record_pk == record.id)))
            if any(self.fresh(s, t, record) and t.status in {"assigned", "in_progress"} for t in active):
                raise ReviewError("An assigned review is still unfinished.", 409)
            decision = s.scalar(
                select(self.ReviewDecision)
                .where(
                    self.ReviewDecision.record_pk == record.id,
                    self.ReviewDecision.input_version == record.input_version,
                    self.ReviewDecision.policy_id == policy.id,
                )
                .order_by(self.ReviewDecision.created_at.desc())
            )
            same = len({digest(x.content) for x in subs}) == 1
            if not same and (not decision or decision.submission_ids != sorted(x.id for x in subs)):
                raise ReviewError("Independent submissions disagree. Record an adjudication first.", 409)
            data = (
                decision.content
                if decision and decision.submission_ids == sorted(x.id for x in subs)
                else subs[0].content
            )
            data = self.checked(data, record, snapshot, candidate.content, True)
            approval = self.ReviewApproval(
                record_pk=record.id,
                input_version=record.input_version,
                policy_id=policy.id,
                actor_id=actor.id,
                payload={
                    "content": data,
                    "sources": snapshot,
                    "submission_ids": [x.id for x in subs],
                    "decision_id": decision.id if decision else None,
                    "candidate_id": candidate.id,
                    "candidate_provenance": candidate.provenance,
                    "policy": policy.settings,
                },
            )
            s.add(approval)
            s.flush()
            record.approved_id = approval.id
            record.state = "approved"
            self.audit(s, actor, "record_approved", approval.id, {"record_id": record_id})
            return approval.id

    def release_metadata(self, s, payload):
        return payload

    def release(self, actor_id, record_ids):
        if not record_ids:
            raise ReviewError("Select at least one approved record.")
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            records = list(
                s.scalars(
                    select(self.Record).where(self.Record.project_id == self.project_id).with_for_update()
                )
            )
            selected = [r for r in records if r.record_id in set(record_ids)]
            if len(selected) != len(set(record_ids)):
                raise ReviewError("Unknown record in release.")
            approved = []
            for record in selected:
                a = s.get(self.ReviewApproval, record.approved_id) if record.approved_id else None
                c = self.latest(s, record)
                if (
                    not a
                    or record.state != "approved"
                    or record.active_run_id
                    or a.input_version != record.input_version
                    or a.policy_id != self.policy(s).id
                    or not c
                    or a.payload["candidate_id"] != c.id
                ):
                    raise ReviewError("Every selected record must have a current approval.", 409)
                approved.append(
                    {
                        "record_id": record.record_id,
                        "approval_id": a.id,
                        "input_version": a.input_version,
                        "approved_by": a.actor_id,
                        "approved_at": a.created_at,
                        **a.payload,
                    }
                )
            history = []
            for record in selected:
                tasks = list(s.scalars(select(self.ReviewTask).where(self.ReviewTask.record_pk == record.id)))
                task_ids = [t.id for t in tasks]
                history.append(
                    {
                        "record_id": record.record_id,
                        "tasks": [
                            {
                                "id": t.id,
                                "assignee_id": t.assignee_id,
                                "input_version": t.input_version,
                                "candidate_id": t.candidate_id,
                                "policy_id": t.policy_id,
                                "status": t.status,
                                "revision": t.revision,
                                "due_at": t.due_at,
                            }
                            for t in tasks
                        ],
                        "edits": [
                            {
                                "task_id": e.task_id,
                                "actor_id": e.actor_id,
                                "revision": e.revision,
                                "content": e.content,
                                "reason": e.reason,
                                "created_at": e.created_at,
                            }
                            for e in s.scalars(
                                select(self.ReviewEdit).where(self.ReviewEdit.task_id.in_(task_ids))
                            )
                        ],
                        "submissions": [
                            {
                                "id": x.id,
                                "task_id": x.task_id,
                                "reviewer_id": x.reviewer_id,
                                "content": x.content,
                                "created_at": x.created_at,
                            }
                            for x in s.scalars(
                                select(self.ReviewSubmission).where(
                                    self.ReviewSubmission.task_id.in_(task_ids)
                                )
                            )
                        ],
                        "decisions": [
                            {
                                "id": x.id,
                                "actor_id": x.actor_id,
                                "content": x.content,
                                "reason": x.reason,
                                "submission_ids": x.submission_ids,
                            }
                            for x in s.scalars(
                                select(self.ReviewDecision).where(self.ReviewDecision.record_pk == record.id)
                            )
                        ],
                        "discussions": [
                            {
                                "question": x.question,
                                "requested_by": x.requested_by,
                                "helper_id": x.helper_id,
                                "messages": x.messages,
                                "status": x.status,
                            }
                            for x in s.scalars(
                                select(self.ReviewHelp).where(self.ReviewHelp.task_id.in_(task_ids))
                            )
                        ],
                    }
                )
            payload = {
                "review_history": history,
                "actors": [
                    {"id": u.id, "name": u.name, "role": u.role}
                    for u in s.scalars(select(ReviewUser).where(ReviewUser.project_id == self.project_id))
                ],
                "research_approved": True,
                "records": approved,
                "processing_report": [
                    {
                        "record_id": r.record_id,
                        "input_version": r.input_version,
                        "state": r.state,
                        "included": r.record_id in set(record_ids),
                    }
                    for r in records
                ],
            }
            payload = self.release_metadata(s, payload)
            release = ReviewRelease(
                project_id=self.project_id, actor_id=actor.id, payload=payload, checksum=digest(payload)
            )
            s.add(release)
            s.flush()
            self.audit(
                s, actor, "release_created", release.id, {"records": record_ids, "checksum": release.checksum}
            )
            return release.id

    def request_help(self, actor_id, task_id, question):
        if not question.strip():
            raise ReviewError("Describe the field or uncertainty you need help with.")
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id)
            task = s.get(self.ReviewTask, task_id)
            if not task:
                raise ReviewError("Task not found.", 404)
            self.editable(s, actor, task.id, task.revision)
            help_item = self.ReviewHelp(task_id=task.id, requested_by=actor.id, question=question.strip())
            s.add(help_item)
            s.flush()
            self.audit(s, actor, "help_requested", help_item.id)
            return help_item.id

    def assign_help(self, actor_id, help_id, helper_id):
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            helper = self.actor(s, helper_id)
            h = s.get(self.ReviewHelp, help_id)
            t = s.get(self.ReviewTask, h.task_id) if h else None
            if not t or s.get(self.Record, t.record_pk).project_id != self.project_id:
                raise ReviewError("Help request not found.", 404)
            if helper.id == h.requested_by or s.scalar(
                select(self.ReviewTask).where(
                    self.ReviewTask.record_pk == t.record_pk,
                    self.ReviewTask.input_version == t.input_version,
                    self.ReviewTask.assignee_id == helper.id,
                    self.ReviewTask.status.notin_(["cancelled", "stale", "returned"]),
                )
            ):
                raise ReviewError("Choose a helper who is not an independent reviewer of this record.")
            h.helper_id = helper.id
            h.status = "assigned"
            self.audit(s, actor, "help_assigned", h.id, {"helper_id": helper.id})
            self.audit(
                s,
                helper,
                "review_answers_viewed",
                t.record_pk,
                {"input_version": t.input_version, "kind": "discussion_helper"},
            )

    def help_reply(self, actor_id, help_id, message, close=False):
        if not message.strip():
            raise ReviewError("A message is required.")
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id)
            h = s.get(self.ReviewHelp, help_id)
            t = s.get(self.ReviewTask, h.task_id) if h else None
            if (
                not t
                or s.get(self.Record, t.record_pk).project_id != self.project_id
                or (actor.role != "owner" and actor.id not in {h.requested_by, h.helper_id})
            ):
                raise ReviewError("Help request is not accessible.", 403)
            self.access(s, actor, s.get(self.Record, t.record_pk))
            h.messages = [
                *h.messages,
                {"actor_id": actor.id, "name": actor.name, "text": message.strip(), "at": now()},
            ]
            if close:
                if actor.id != h.requested_by and actor.role != "owner":
                    raise ReviewError("Only the requester or owner can close this discussion.", 403)
                h.status = "closed"
            self.audit(s, actor, "help_replied", h.id, {"closed": close})

    def exclude(self, actor_id, record_id, reason):
        if not reason.strip():
            raise ReviewError("An exclusion reason is required.")
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id, True)
            record = self.record(s, record_id)
            record.state = "excluded"
            record.approved_id = None
            for t in s.scalars(select(self.ReviewTask).where(self.ReviewTask.record_pk == record.id)):
                if t.status in {"assigned", "in_progress"}:
                    t.status = "cancelled"
            self.audit(
                s,
                actor,
                "record_excluded",
                record.id,
                {"reason": reason, "input_version": record.input_version},
            )

    def members(self, actor_id):
        with self.factory() as s:
            self.actor(s, actor_id, True)
            return [
                {
                    "id": u.id,
                    "name": u.name,
                    "email": u.email,
                    "role": u.role,
                    "active": bool(u.active),
                    "accepted": bool(u.password_hash),
                }
                for u in s.scalars(select(ReviewUser).where(ReviewUser.project_id == self.project_id))
            ]

    def queue(self, actor_id):
        with self.factory() as s:
            actor = self.actor(s, actor_id)
            rows = []
            for r in s.scalars(
                select(self.Record)
                .where(self.Record.project_id == self.project_id, self.Record.state != "retired")
                .order_by(self.Record.record_id)
            ):
                tasks = list(s.scalars(select(self.ReviewTask).where(self.ReviewTask.record_pk == r.id)))
                visible = actor.role == "owner" or any(t.assignee_id == actor.id for t in tasks)
                if not visible:
                    try:
                        self.access(s, actor, r)
                        visible = True
                    except ReviewError:
                        pass
                if not visible:
                    continue
                candidate = self.latest(s, r)
                snap = self.snapshot(s, r)
                my_tasks = [
                    {
                        "id": t.id,
                        "status": t.status if self.fresh(s, t, r) else "stale",
                        "assignee_id": t.assignee_id,
                    }
                    for t in tasks
                    if actor.role == "owner" or t.assignee_id == actor.id
                ]
                current_subs = self.current_submissions(s, r)
                required = self.required(r, candidate, self.policy(s), snap) if candidate else 1
                state = r.state
                if (
                    r.state != "excluded"
                    and candidate
                    and len(current_subs) >= required
                    and not r.approved_id
                ):
                    state = (
                        "needs_adjudication"
                        if len({digest(x.content) for x in current_subs}) > 1
                        else "ready_for_approval"
                    )
                if r.approved_id:
                    a = s.get(self.ReviewApproval, r.approved_id)
                    if (
                        a.policy_id != self.policy(s).id
                        or not candidate
                        or a.payload["candidate_id"] != candidate.id
                    ):
                        state = "pending_review"
                rows.append(
                    {
                        "record_id": r.record_id,
                        "input_version": r.input_version,
                        "state": state,
                        "pages": sum(d["page_count"] for d in snap["documents"].values()),
                        "attachments": sum(d["role"] != "main" for d in snap["documents"].values()),
                        "has_candidate": bool(candidate),
                        "issues": len(candidate.content.get("issues", [])) if candidate else 0,
                        "required_reviews": required,
                        "submitted": len(current_subs),
                        "tasks": my_tasks,
                    }
                )
            return rows

    def detail(self, actor_id, record_id):
        with self.factory.begin() as s:
            actor = self.actor(s, actor_id)
            record = self.record(s, record_id)
            self.access(s, actor, record)
            candidate = self.latest(s, record)
            tasks = list(s.scalars(select(self.ReviewTask).where(self.ReviewTask.record_pk == record.id)))
            own = [
                t
                for t in tasks
                if t.assignee_id == actor.id
                and t.status not in {"cancelled", "returned", "stale"}
                and self.fresh(s, t, record)
            ]
            task = own[-1] if own else None
            blind = bool(task and task.status != "submitted")
            subs = self.current_submissions(s, record) if actor.role == "owner" and not blind else []
            if any(x.reviewer_id != actor.id for x in subs):
                self.audit(
                    s,
                    actor,
                    "review_answers_viewed",
                    record.id,
                    {"input_version": record.input_version, "kind": "owner_comparison"},
                )
            snapshot = self.snapshot(s, record)
            help_items = []
            for h in s.scalars(
                select(self.ReviewHelp).where(self.ReviewHelp.task_id.in_([t.id for t in tasks]))
            ):
                if actor.role == "owner" or actor.id in {h.requested_by, h.helper_id}:
                    help_items.append(
                        {
                            "id": h.id,
                            "task_id": h.task_id,
                            "question": h.question,
                            "helper_id": h.helper_id,
                            "requested_by": h.requested_by,
                            "messages": h.messages,
                            "status": h.status,
                        }
                    )
            decision = (
                s.scalar(
                    select(self.ReviewDecision)
                    .where(
                        self.ReviewDecision.record_pk == record.id,
                        self.ReviewDecision.input_version == record.input_version,
                        self.ReviewDecision.policy_id == self.policy(s).id,
                    )
                    .order_by(self.ReviewDecision.created_at.desc())
                )
                if actor.role == "owner" and not blind
                else None
            )
            approval = s.get(self.ReviewApproval, record.approved_id) if record.approved_id else None
            display_content = task.draft if task else candidate.content if candidate else None
            display_mode = (
                "submitted_review"
                if task and task.status == "submitted"
                else "review_draft"
                if task
                else "ai_draft"
            )
            if actor.role == "owner" and not blind:
                if (
                    approval
                    and approval.input_version == record.input_version
                    and candidate
                    and approval.payload["candidate_id"] == candidate.id
                    and approval.policy_id == self.policy(s).id
                ):
                    display_content, display_mode = approval.payload["content"], "approved"
                elif decision:
                    display_content, display_mode = decision.content, "adjudicated"
            if display_content is not None:
                display_content = refresh_issues(
                    RecordContent.model_validate(copy.deepcopy(display_content)),
                    snapshot,
                    candidate.content if candidate else None,
                ).model_dump(mode="json")
            return {
                "approval_blocking_codes": sorted(APPROVAL_BLOCKING_CODES),
                "display_mode": display_mode,
                "record_id": record_id,
                "input_version": record.input_version,
                "snapshot": snapshot,
                "candidate_id": candidate.id if candidate else None,
                "content": display_content,
                "task": ({"id": task.id, "revision": task.revision, "status": task.status} if task else None),
                "tasks": [
                    {
                        "id": t.id,
                        "assignee_id": t.assignee_id,
                        "status": t.status if self.fresh(s, t, record) else "stale",
                        "revision": t.revision,
                    }
                    for t in tasks
                    if actor.role == "owner" or t.assignee_id == actor.id
                ],
                "required_reviews": self.required(record, candidate, self.policy(s), snapshot)
                if candidate
                else 1,
                "submissions": [
                    {
                        "id": x.id,
                        "reviewer_id": x.reviewer_id,
                        "content": x.content,
                        "created_at": x.created_at,
                    }
                    for x in subs
                ],
                "decision": (
                    {"id": decision.id, "content": decision.content, "reason": decision.reason}
                    if decision
                    else None
                ),
                "help": help_items,
                "blind_review": blind,
                "approved_id": record.approved_id,
            }
