"""Portable relational persistence; PostgreSQL is the team deployment target."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, synonym


def uid() -> str:
    return str(uuid4())


def now() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)


class Record(Base):
    __tablename__ = "records"
    __table_args__ = (UniqueConstraint("project_id", "record_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    record_id: Mapped[str] = mapped_column(String)
    input_version: Mapped[int] = mapped_column(Integer, default=0)
    fingerprint: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String, default="missing_source")
    required_reviews: Mapped[int] = mapped_column(Integer, default=1)
    active_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_id: Mapped[str | None] = mapped_column(String, nullable=True)
    __mapper_args__ = {"version_id_col": input_version, "version_id_generator": False}


class InputRevision(Base):
    __tablename__ = "input_revisions"
    __table_args__ = (UniqueConstraint("record_pk", "version"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    record_pk: Mapped[str] = mapped_column(ForeignKey("records.id"))
    version: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String)
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    manifest: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    record_pk: Mapped[str] = mapped_column(ForeignKey("records.id"))
    input_version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    provenance: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    actor_id: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    target_id: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    record_pk: Mapped[str] = mapped_column(ForeignKey("records.id"))
    input_version: Mapped[int] = mapped_column(Integer)
    cache_key: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="running")
    configuration: Mapped[dict] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    candidate_id: Mapped[str | None] = mapped_column(String, nullable=True)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    elapsed_seconds: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)


def database(url: str):
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


# Additive P2 tables: existing P0/P1 snapshots remain unchanged.
class ReviewUser(Base):
    __tablename__ = "review_users"
    __table_args__ = (UniqueConstraint("project_id", "email"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    email: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[int] = mapped_column(Integer, default=1)
    invite_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    invite_expires: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ReviewSession(Base):
    __tablename__ = "review_sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    csrf: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)


class ReviewPolicy(Base):
    __tablename__ = "review_policies"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    settings: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    created_at: Mapped[str] = mapped_column(String, default=now)


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    record_pk: Mapped[str] = mapped_column(ForeignKey("records.id"))
    input_version: Mapped[int] = mapped_column(Integer)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"))
    policy_id: Mapped[str] = mapped_column(ForeignKey("review_policies.id"))
    assignee_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    status: Mapped[str] = mapped_column(String, default="assigned")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    draft: Mapped[dict] = mapped_column(JSON)
    due_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __mapper_args__ = {"version_id_col": revision}


class ReviewEdit(Base):
    __tablename__ = "review_edits"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ReviewSubmission(Base):
    __tablename__ = "review_submissions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id"), unique=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ReviewHelp(Base):
    __tablename__ = "review_help"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id"))
    requested_by: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    helper_id: Mapped[str | None] = mapped_column(ForeignKey("review_users.id"), nullable=True)
    question: Mapped[str] = mapped_column(String)
    messages: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="requested")
    created_at: Mapped[str] = mapped_column(String, default=now)


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    record_pk: Mapped[str] = mapped_column(ForeignKey("records.id"))
    input_version: Mapped[int] = mapped_column(Integer)
    policy_id: Mapped[str] = mapped_column(ForeignKey("review_policies.id"))
    submission_ids: Mapped[list] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    content: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ReviewApproval(Base):
    __tablename__ = "review_approvals"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    record_pk: Mapped[str] = mapped_column(ForeignKey("records.id"))
    input_version: Mapped[int] = mapped_column(Integer)
    policy_id: Mapped[str] = mapped_column(ForeignKey("review_policies.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ReviewRelease(Base):
    __tablename__ = "review_releases"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    payload: Mapped[dict] = mapped_column(JSON)
    checksum: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)


class DayPlacement(Base):
    """Version-bound assignment of one source-record day to a participant/date."""

    __tablename__ = "day_placements"
    __table_args__ = (UniqueConstraint("record_pk", "input_version", "candidate_key", "day_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    record_pk: Mapped[str] = mapped_column(ForeignKey("records.id"))
    input_version: Mapped[int] = mapped_column(Integer)
    candidate_key: Mapped[str] = mapped_column(String)
    day_id: Mapped[str] = mapped_column(String)
    subject_id: Mapped[str | None] = mapped_column(String, nullable=True)
    date: Mapped[str | None] = mapped_column(String, nullable=True)
    proposed: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String, default="")
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __mapper_args__ = {"version_id_col": revision}


class DayReviewUnit(Base):
    __tablename__ = "day_review_units"
    __table_args__ = (UniqueConstraint("project_id", "subject_id", "date"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    subject_id: Mapped[str] = mapped_column(String)
    date: Mapped[str] = mapped_column(String)
    input_version: Mapped[int] = mapped_column(Integer, default=0)
    fingerprint: Mapped[str] = mapped_column(String, default="")
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String, default="pending_review")
    approved_id: Mapped[str | None] = mapped_column(String, nullable=True)
    required_reviews: Mapped[int] = mapped_column(Integer, default=1)
    active_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)
    record_id = synonym("id")
    __mapper_args__ = {"version_id_col": input_version, "version_id_generator": False}


class DayReviewTask(Base):
    __tablename__ = "day_review_tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    record_pk: Mapped[str] = mapped_column(ForeignKey("day_review_units.id"))
    input_version: Mapped[int] = mapped_column(Integer)
    candidate_id: Mapped[str] = mapped_column(String)
    policy_id: Mapped[str] = mapped_column(ForeignKey("review_policies.id"))
    assignee_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    status: Mapped[str] = mapped_column(String, default="assigned")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    draft: Mapped[dict] = mapped_column(JSON)
    due_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __mapper_args__ = {"version_id_col": revision}


class DayReviewEdit(Base):
    __tablename__ = "day_review_edits"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("day_review_tasks.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)


class DayReviewSubmission(Base):
    __tablename__ = "day_review_submissions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("day_review_tasks.id"), unique=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class DayReviewHelp(Base):
    __tablename__ = "day_review_help"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("day_review_tasks.id"))
    requested_by: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    helper_id: Mapped[str | None] = mapped_column(ForeignKey("review_users.id"), nullable=True)
    question: Mapped[str] = mapped_column(String)
    messages: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="requested")
    created_at: Mapped[str] = mapped_column(String, default=now)


class DayReviewDecision(Base):
    __tablename__ = "day_review_decisions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    record_pk: Mapped[str] = mapped_column(ForeignKey("day_review_units.id"))
    input_version: Mapped[int] = mapped_column(Integer)
    policy_id: Mapped[str] = mapped_column(ForeignKey("review_policies.id"))
    submission_ids: Mapped[list] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    content: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)


class DayReviewApproval(Base):
    __tablename__ = "day_review_approvals"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    record_pk: Mapped[str] = mapped_column(ForeignKey("day_review_units.id"))
    input_version: Mapped[int] = mapped_column(Integer)
    policy_id: Mapped[str] = mapped_column(ForeignKey("review_policies.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class OfflinePackage(Base):
    __tablename__ = "offline_packages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    manifest: Mapped[dict] = mapped_column(JSON)
    archive_sha256: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)


class OfflineReceipt(Base):
    __tablename__ = "offline_receipts"
    __table_args__ = (UniqueConstraint("package_id", "result_sha256"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    package_id: Mapped[str] = mapped_column(ForeignKey("offline_packages.id"))
    result_sha256: Mapped[str] = mapped_column(String)
    imported_by: Mapped[str] = mapped_column(ForeignKey("review_users.id"))
    outcome: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class OfflineSettings(Base):
    __tablename__ = "offline_settings"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    units_per_package: Mapped[int] = mapped_column(Integer, default=10)
