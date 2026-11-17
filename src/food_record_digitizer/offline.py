"""Manager-side offline assignment ledger and transactional result intake."""

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from . import db
from .day_grouping import sync_day_units
from .day_review import DayReviewService
from .imaging import render_pages
from .offline_archive import FORMAT_VERSION, batch_units, canonical, pack, sha256, unpack
from .review import ReviewError
from .schemas import RecordContent


def retain(root: Path, data: bytes):
    """Content-addressed, atomic private storage; failed DB writes leave harmless orphans."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / (sha256(data) + ".zip")
    if target.exists():
        if sha256(target.read_bytes()) != sha256(data):
            raise ReviewError("Stored package checksum mismatch.")
        return target
    fd, name = tempfile.mkstemp(dir=root)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return target


class OfflineManager:
    def __init__(self, workspace):
        self.workspace = workspace
        self.service = DayReviewService(workspace)
        self.root = workspace.path / "offline"

    def reviewer(self, actor_id, name, email):
        """Register attribution without requiring an invitation or online password."""
        name, email = name.strip(), email.strip().lower()
        if not name or not email or "@" not in email:
            raise ReviewError("Enter a reviewer name and email.")
        with self.workspace.factory.begin() as s:
            self.service.actor(s, actor_id, True)
            member = s.scalar(
                select(db.ReviewUser).where(
                    db.ReviewUser.project_id == self.workspace.project_id, db.ReviewUser.email == email
                )
            )
            if member:
                if not member.active:
                    raise ReviewError("This reviewer is inactive.")
                return member.id
            member = db.ReviewUser(
                project_id=self.workspace.project_id, name=name, email=email, role="reviewer"
            )
            s.add(member)
            s.flush()
            return member.id

    def select_next(self, actor_id, reviewer_id, count=100, independent=False, scope=None):
        """Preview a deterministic batch without assigning or exporting anything."""
        if type(count) is not int or not 1 <= count <= 1000 or type(independent) is not bool:
            raise ReviewError("Choose between 1 and 1000 days.")
        svc = self.service
        with self.workspace.factory() as s:
            svc.actor(s, actor_id, True)
            svc.actor(s, reviewer_id)
        rows = svc.queue(actor_id)
        allowed = set(scope) if scope is not None else None
        selected, skipped = [], []
        with self.workspace.factory() as s:
            for row in rows:
                if allowed is not None and row["record_id"] not in allowed:
                    continue
                current = [t for t in row["tasks"] if t["status"] not in {"stale", "returned", "cancelled"}]
                if any(t["assignee_id"] == reviewer_id for t in current):
                    continue
                if current and not independent:
                    continue
                if not row["has_candidate"]:
                    continue
                try:
                    self.prepare(s, actor_id, reviewer_id, [row["record_id"]], 1)
                except ReviewError as exc:
                    skipped.append({"record_id": row["record_id"], "reason": str(exc)})
                    continue
                selected.append(row)
                if len(selected) == count:
                    break
        return {
            "unit_ids": [r["record_id"] for r in selected],
            "days": len(selected),
            "records": len({sid for r in selected for sid in r["source_record_ids"]}),
            "skipped": skipped,
        }

    def prepare(self, s, actor_id, reviewer_id, unit_ids, units_per_package):
        svc = self.service
        actor = svc.actor(s, actor_id, True)
        reviewer = svc.actor(s, reviewer_id)
        if not unit_ids:
            raise ReviewError("Select at least one day.")
        units = []
        for unit_id in unit_ids:
            unit = svc.record(s, unit_id)
            snapshot = svc.snapshot(s, unit)
            label = f"{unit.subject_id} · {svc.labels(unit)['date'] or 'Unknown date'}"
            if unit.state in {"excluded", "retired", "approved"} or unit.active_run_id:
                raise ReviewError(f"{label}: Select current, unapproved days without running extraction. Wait for processing or remove this day from the selection.")
            if snapshot.get("pending_placements"):
                raise ReviewError(f"{label}: Confirm the participant in Edit metadata before sending. Unknown dates may remain blank.")
            if any(d.get("error") for d in snapshot["documents"].values()):
                raise ReviewError(f"{label}: A source cannot be read. Check the scans in this day and restore or replace the missing file.")
            if any(m["record_id"] not in snapshot["documents"] for m in snapshot["members"]):
                raise ReviewError(f"{label}: A main scan is missing. Restore it before sending this day.")
            if set(snapshot["metadata"]) - set(snapshot["documents"]):
                raise ReviewError(f"{label}: A source listed in metadata is missing. Check this day’s sources before sending.")
            tasks = list(s.scalars(select(db.DayReviewTask).where(db.DayReviewTask.record_pk == unit.id)))
            existing = next(
                (
                    t
                    for t in tasks
                    if t.assignee_id == reviewer.id
                    and t.status not in {"cancelled", "returned", "stale"}
                    and svc.fresh(s, t, unit)
                ),
                None,
            )
            if existing and existing.status != "assigned":
                raise ReviewError(
                    f"{unit.subject_id} · {svc.labels(unit)['date'] or 'Unknown date'}: this reviewer has started or completed this day. Finish or return the current task before creating a new package.",
                    409,
                )
            if existing:
                issued_packages = s.scalars(
                    select(db.OfflinePackage).where(
                        db.OfflinePackage.project_id == self.workspace.project_id,
                        db.OfflinePackage.reviewer_id == reviewer.id,
                    )
                )
                if any(
                    any(u["task_id"] == existing.id for u in p.manifest["units"]) for p in issued_packages
                ):
                    raise ReviewError(
                        f"{label}: This task is already included in a review package. Download the existing package from Review packages, or return the task before issuing a replacement.",
                        409,
                    )
            current_ids = [t.id for t in tasks if svc.fresh(s, t, unit)]
            if current_ids and s.scalar(
                select(db.DayReviewHelp).where(
                    db.DayReviewHelp.task_id.in_(current_ids), db.DayReviewHelp.helper_id == reviewer.id
                )
            ):
                raise ReviewError(f"{label}: A discussion helper cannot independently review this day. Choose another reviewer.", 409)
            source_versions = {m["record_pk"]: m["input_version"] for m in snapshot["members"]}
            source_versions[unit.id] = unit.input_version
            for exposure in s.scalars(
                select(db.Audit).where(
                    db.Audit.project_id == self.workspace.project_id,
                    db.Audit.actor_id == reviewer.id,
                    db.Audit.action == "review_answers_viewed",
                    db.Audit.target_id.in_(source_versions),
                )
            ):
                if exposure.detail.get("input_version") == source_versions[exposure.target_id]:
                    raise ReviewError(f"{label}: This reviewer has already seen another review. Choose an independent reviewer.", 409)
            units.append(
                {
                    "id": unit.id,
                    "subject_id": unit.subject_id,
                    "date": svc.labels(unit)["date"] or "",
                    "input_version": unit.input_version,
                    "fingerprint": unit.fingerprint,
                    "snapshot": copy.deepcopy(snapshot),
                    "content": copy.deepcopy(existing.draft if existing else unit.content),
                    "existing_task_id": existing.id if existing else None,
                }
            )
        return actor, reviewer, batch_units(units, units_per_package)

    def export(self, actor_id, reviewer_id, unit_ids, units_per_package=None, preview=False):
        svc = self.service
        with self.workspace.factory() as s:
            svc.actor(s, actor_id, True)
        with svc._sync_lock:
            sync_day_units(svc)
            with self.workspace.factory.begin() as s:
                s.scalar(
                    select(db.Project).where(db.Project.id == self.workspace.project_id).with_for_update()
                )
                settings = s.get(db.OfflineSettings, self.workspace.project_id)
                if units_per_package is None:
                    units_per_package = settings.units_per_package if settings else 10
                svc.actor(s, reviewer_id)
                remaining = set(unit_ids)
                results = []
                for package in s.scalars(
                    select(db.OfflinePackage)
                    .where(
                        db.OfflinePackage.project_id == self.workspace.project_id,
                        db.OfflinePackage.reviewer_id == reviewer_id,
                    )
                    .order_by(db.OfflinePackage.created_at.desc())
                ):
                    issued = package.manifest["units"]
                    packaged_ids = {u["id"] for u in issued}
                    if not remaining.intersection(packaged_ids):
                        continue
                    current = all(
                        (task := s.get(db.DayReviewTask, u["task_id"])) is not None
                        and task.status in {"assigned", "in_progress"}
                        and (unit := s.get(db.DayReviewUnit, u["id"])) is not None
                        and svc.fresh(s, task, unit)
                        for u in issued
                    )
                    if not current:
                        continue
                    if not packaged_ids.issubset(remaining):
                        raise ReviewError(
                            "Some selected days are already in a larger review package. Download that existing package from Review packages, or select all of its days.",
                            409,
                        )
                    path = self.root / "packages" / (package.archive_sha256 + ".zip")
                    if not path.is_file() or sha256(path.read_bytes()) != package.archive_sha256:
                        raise ReviewError(
                            "The existing package file is missing or damaged. Restore a study backup.", 409
                        )
                    results.append(
                        {
                            "package_id": package.id,
                            "days": len(issued),
                            "unit_ids": sorted(packaged_ids),
                            "path": str(path),
                            "reused": True,
                        }
                    )
                    remaining -= packaged_ids
                if not remaining and results:
                    return results
                actor, reviewer, batches = self.prepare(
                    s,
                    actor_id,
                    reviewer_id,
                    list(dict.fromkeys(i for i in unit_ids if i in remaining)),
                    units_per_package,
                )
                policy = svc.policy(s)
                for units in batches:
                    docs = {d["sha256"] for u in units for d in u["snapshot"]["documents"].values()}
                    paths = [self.workspace.store.verified_path(h) for h in sorted(docs)]
                    summary = {
                        "days": len(units),
                        "pages": sum(
                            d["page_count"] for u in units for d in u["snapshot"]["documents"].values()
                        ),
                        "source_bytes": sum(p.stat().st_size for p in paths),
                        "unit_ids": [u["id"] for u in units],
                    }
                    if preview:
                        results.append(summary)
                        continue
                    package_id = db.uid()
                    for u in units:
                        existing_id = u.pop("existing_task_id", None)
                        task = s.get(db.DayReviewTask, existing_id) if existing_id else None
                        if task is None:
                            task = db.DayReviewTask(
                                id=db.uid(),
                                record_pk=u["id"],
                                input_version=u["input_version"],
                                candidate_id=u["fingerprint"],
                                policy_id=policy.id,
                                assignee_id=reviewer.id,
                                draft=copy.deepcopy(u["content"]),
                            )
                            s.add(task)
                        u["task_id"] = task.id
                        u["baseline_sha256"] = sha256(canonical(u["content"]))
                        record = s.get(db.DayReviewUnit, u["id"])
                        record.state = "in_review"
                        record.approved_id = None
                    manifest = {
                        "kind": "task",
                        "format_version": FORMAT_VERSION,
                        "package_id": package_id,
                        "project_id": self.workspace.project_id,
                        "created_at": db.now(),
                        "reviewer": {"id": reviewer.id, "name": reviewer.name, "email": reviewer.email},
                        "units_per_package": units_per_package,
                        "policy": {"id": policy.id, "settings": policy.settings},
                        "units": units,
                    }
                    preview_blobs = []
                    previews = {}
                    for u in units:
                        for page in render_pages(
                            self.workspace.store, u["snapshot"], self.workspace.path / "rendered", 150
                        ):
                            blob = Path(page["path"]).read_bytes()
                            source_hash = u["snapshot"]["documents"][page["source_record_id"]]["sha256"]
                            previews.setdefault(source_hash, {})[str(page["page_number"])] = sha256(blob)
                            preview_blobs.append(blob)
                    manifest["previews"] = previews
                    data = pack(manifest, [p.read_bytes() for p in paths] + preview_blobs)
                    path = retain(self.root / "packages", data)
                    manifest, _ = unpack(data, "task")
                    s.add(
                        db.OfflinePackage(
                            id=package_id,
                            project_id=self.workspace.project_id,
                            reviewer_id=reviewer.id,
                            created_by=actor.id,
                            manifest=manifest,
                            archive_sha256=sha256(data),
                        )
                    )
                    svc.audit(s, actor, "offline_package_exported", package_id, summary)
                    results.append({**summary, "package_id": package_id, "path": str(path)})
                if not preview:
                    if settings is None:
                        s.add(
                            db.OfflineSettings(
                                project_id=self.workspace.project_id, units_per_package=units_per_package
                            )
                        )
                    else:
                        settings.units_per_package = units_per_package
                return results

    def import_result(self, actor_id, data):
        svc = self.service
        with self.workspace.factory() as s:
            svc.actor(s, actor_id, True)
        result, blobs = unpack(data, "result")
        if blobs:
            raise ReviewError("Result packages must not include source files.")
        for key in ("package_id", "project_id", "reviewer_id", "task_archive_sha256"):
            if not isinstance(result.get(key), str) or not result[key]:
                raise ReviewError("Result package identity is invalid.")
        digest = sha256(data)
        with svc._sync_lock:
            sync_day_units(svc)
            with self.workspace.factory.begin() as s:
                s.scalar(
                    select(db.Project).where(db.Project.id == self.workspace.project_id).with_for_update()
                )
                actor = svc.actor(s, actor_id, True)
                package = s.get(db.OfflinePackage, result.get("package_id", ""))
                if not package or package.project_id != self.workspace.project_id:
                    raise ReviewError("This package was not issued by this project.")
                receipt = s.scalar(
                    select(db.OfflineReceipt).where(
                        db.OfflineReceipt.package_id == package.id, db.OfflineReceipt.result_sha256 == digest
                    )
                )
                if receipt:
                    return {"status": "duplicate", "receipt_id": receipt.id, "original": receipt.outcome}
                issued = package.manifest
                if (
                    result.get("project_id") != package.project_id
                    or result.get("reviewer_id") != package.reviewer_id
                    or result.get("task_archive_sha256") != package.archive_sha256
                ):
                    raise ReviewError("Result identity or baseline does not match the issued package.")
                returns = result.get("units")
                expected = {u["task_id"]: u for u in issued["units"]}
                if (
                    not isinstance(returns, list)
                    or any(not isinstance(u, dict) or not isinstance(u.get("task_id"), str) for u in returns)
                    or len(returns) != len(expected)
                    or {u.get("task_id") for u in returns} != set(expected)
                ):
                    raise ReviewError("Return every assigned day exactly once.")
                outcomes = []
                accepted = []
                for returned in returns:
                    original = expected[returned["task_id"]]
                    if returned.get("baseline_sha256") != original["baseline_sha256"]:
                        raise ReviewError("Task baseline mismatch.")
                    content = RecordContent.model_validate(returned.get("content")).model_dump(mode="json")
                    task = s.get(db.DayReviewTask, original["task_id"])
                    record = s.get(db.DayReviewUnit, original["id"])
                    if not task or not record:
                        raise ReviewError("Issued task history is missing.")
                    old = s.scalar(
                        select(db.DayReviewSubmission).where(db.DayReviewSubmission.task_id == task.id)
                    )
                    if old:
                        status = "duplicate" if canonical(old.content) == canonical(content) else "conflict"
                    elif (
                        task.status not in {"assigned", "in_progress"}
                        or not svc.fresh(s, task, record)
                        or not s.get(db.ReviewUser, package.reviewer_id).active
                        or record.fingerprint != original["fingerprint"]
                    ):
                        status = "stale"
                    else:
                        content = svc.checked(
                            content, record, svc.snapshot(s, record), original["content"], final=True
                        )
                        status = "accepted"
                        accepted.append((task, content))
                    outcomes.append({"task_id": task.id, "unit_id": record.id, "status": status})
                # Reject malformed packages before any authoritative submissions are written.
                retain(self.root / "results", data)
                for task, content in accepted:
                    task.draft = content
                    task.status = "submitted"
                    s.flush()
                    s.add(
                        db.DayReviewEdit(
                            task_id=task.id,
                            actor_id=package.reviewer_id,
                            revision=task.revision,
                            content=content,
                            reason="Offline review returned",
                        )
                    )
                    s.add(
                        db.DayReviewSubmission(
                            task_id=task.id, reviewer_id=package.reviewer_id, content=content
                        )
                    )
                outcome = {"status": "processed", "tasks": outcomes}
                receipt = db.OfflineReceipt(
                    package_id=package.id, result_sha256=digest, imported_by=actor.id, outcome=outcome
                )
                s.add(receipt)
                s.flush()
                svc.audit(s, actor, "offline_result_imported", receipt.id, outcome)
                return {**outcome, "receipt_id": receipt.id}
