"""Participant/date membership, source-preserving day projections and version reconciliation."""

from __future__ import annotations

import copy
from collections import defaultdict
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

from .db import Audit, Candidate, DayPlacement, DayReviewTask, DayReviewUnit, Project, Record, ReviewTask
from .ingest import current_snapshot, digest
from .schemas import RecordContent, unknown


def iso_date(value):
    try:
        text = str(value or "").strip()
        parsed = date.fromisoformat(text)
        return parsed.isoformat() if parsed.isoformat() == text else None
    except ValueError:
        return None


def subject(value):
    return str(value or "").strip() or None


def proposal(record, candidate, day, snapshot):
    meta = snapshot["metadata"].get(record.record_id, {})
    ms, md = subject(meta.get("subject_id")), iso_date(meta.get("date"))
    cs = subject(candidate.content.get("subject", {}).get("normalized")) if candidate else None
    cd = iso_date(day.get("date", {}).get("normalized"))
    problems = []
    if not ms:
        problems.append("Metadata participant is missing.")
    if not md:
        problems.append("Metadata intake date is missing or invalid.")
    return (
        {
            "metadata_subject": ms,
            "source_subject": cs,
            "metadata_date": md,
            "source_date": cd,
            "problems": problems,
        },
        ms or cs,
        md or cd,
    )


def issue(code, target, message):
    return {"code": code, "target": target, "message": message, "severity": "blocking", "resolution": None}


def project_day(unit_id, sid, day_date, members, pending):
    """Project only selected day events/items; retain all source pages as review context."""
    content = RecordContent(record_id=unit_id).model_dump(mode="json")
    content["days"] = [{"day_id": "intake-day", "date": unknown().model_dump()}]
    snapshot = {
        "documents": {},
        "metadata": {},
        "members": [],
        "entity_origins": {},
        "review_unit": {"id": unit_id, "subject_id": sid, "date": day_date},
        "pending_placements": pending,
    }
    seen_events, seen_items, seen_pages = set(), set(), set()
    for placement, record, candidate, source in members:
        rid = record.record_id
        snapshot["documents"].update(copy.deepcopy(source["documents"]))
        snapshot["metadata"].update(copy.deepcopy(source["metadata"]))
        snapshot["members"].append(
            {
                "record_id": rid,
                "record_pk": record.id,
                "input_version": record.input_version,
                "candidate_id": candidate.id if candidate else None,
                "candidate_provenance": candidate.provenance
                if candidate
                else {"method": "manual_entry_required"},
                "day_id": placement.day_id,
                "placement_id": placement.id,
                "placement_revision": placement.revision,
                "placement_status": placement.status,
                "placement_reason": placement.reason,
                "confirmed_by": placement.confirmed_by,
                "active_run_id": record.active_run_id,
                "required_reviews": record.required_reviews,
                "source_day_count": source.get("day_count", 1),
            }
        )
        raw = (
            copy.deepcopy(candidate.content)
            if candidate
            else RecordContent(record_id=rid).model_dump(mode="json")
        )
        events = [e for e in raw["events"] if e["day_id"] == placement.day_id]
        event_ids = {e["event_id"] for e in events}
        items = [i for i in raw["items"] if i["event_id"] in event_ids]
        item_ids = {i["item_id"] for i in items}
        selected_ids = event_ids | item_ids | {placement.day_id}
        all_ids = {
            x[k]
            for group, k in [("days", "day_id"), ("events", "event_id"), ("items", "item_id")]
            for x in raw[group]
        }

        def reference(key):
            return "intake-day" if key == placement.day_id else rid + "::" + key

        def rewrite(value):
            if isinstance(value, dict):
                if value.get("inherited_from"):
                    value["inherited_from"] = reference(value["inherited_from"])
                for child in value.values():
                    rewrite(child)
            elif isinstance(value, list):
                for child in value:
                    rewrite(child)

        rewrite(raw)
        # Take supported source values only; grouping metadata does not invent transcription evidence.
        if content["subject"]["normalized"] is None and subject(raw["subject"].get("normalized")) == sid:
            content["subject"] = raw["subject"]
        for d in raw["days"]:
            if d["day_id"] == placement.day_id and d["date"].get("normalized") == day_date:
                content["days"][0]["date"] = d["date"]
        meta = source["metadata"].get(rid, {})
        for field, expected, target in [
            ("subject_id", sid, content["subject"]),
            ("date", day_date, content["days"][0]["date"]),
        ]:
            if str(meta.get(field) or "").strip() == expected:
                target.update(
                    normalized=expected,
                    missing_reason=None,
                    evidence=[],
                    inherited_from=None,
                    metadata_reference={"record_id": rid, "field": field},
                )
        if placement.proposed.get("source_subject") not in (None, sid) or placement.proposed.get(
            "source_date"
        ) not in (None, day_date):
            content["issues"].append(
                issue(
                    "metadata_source_difference",
                    rid + "::" + placement.day_id,
                    "The transcription differs from metadata. Metadata determines the participant and intake date; check the food entries against the scan.",
                )
            )
        for e in raw["events"]:
            if e["event_id"] in event_ids:
                original = e["event_id"]
                e["event_id"] = rid + "::" + original
                e["day_id"] = "intake-day"
                if e["event_id"] not in seen_events:
                    content["events"].append(e)
                    seen_events.add(e["event_id"])
                    snapshot["entity_origins"][e["event_id"]] = {
                        "record_id": rid,
                        "day_id": placement.day_id,
                        "entity_id": original,
                    }
        for item in raw["items"]:
            if item["item_id"] in item_ids:
                original = item["item_id"]
                item["item_id"] = rid + "::" + original
                item["event_id"] = rid + "::" + item["event_id"]
                if item["item_id"] not in seen_items:
                    content["items"].append(item)
                    seen_items.add(item["item_id"])
                    snapshot["entity_origins"][item["item_id"]] = {
                        "record_id": rid,
                        "day_id": placement.day_id,
                        "entity_id": original,
                    }
        for raw_issue in raw["issues"]:
            if raw_issue["target"] in all_ids - selected_ids:
                continue
            raw_issue["target"] = rid + "::" + raw_issue["target"]
            content["issues"].append(raw_issue)
        for source_id, doc in source["documents"].items():
            for number in range(1, doc["page_count"] + 1):
                if (source_id, number) in seen_pages:
                    continue
                page = next(
                    (
                        p
                        for p in raw["pages"]
                        if p["source_record_id"] == source_id and p["page_number"] == number
                    ),
                    None,
                )
                page = (
                    copy.deepcopy(page)
                    if page
                    else {
                        "source_record_id": source_id,
                        "page_number": number,
                        "classification": "food_record" if doc["role"] == "main" else "clarification",
                        "regions": [],
                        "notes": "",
                        "source_incomplete": False,
                    }
                )
                page["reviewed"] = False
                content["pages"].append(page)
                seen_pages.add((source_id, number))
        if not candidate or placement.day_id not in {d["day_id"] for d in raw["days"]}:
            content["issues"].append(
                issue(
                    "manual_transcription_required",
                    rid,
                    "This source has no AI draft. Transcribe this date or explain why it adds no intake.",
                )
            )
        if raw.get("notes"):
            content["notes"] += f"{rid}: {raw['notes']}\n"
        if rid not in source["documents"] or any(d.get("error") for d in source["documents"].values()):
            content["issues"].append(issue("missing_main", rid, "A readable main source is required."))
        for missing in set(source["metadata"]) - set(source["documents"]):
            content["issues"].append(
                issue("missing_source", missing, "A listed source scan has not been supplied.")
            )
        if source.get("day_count", len(raw["days"])) > 1:
            content["issues"].append(
                issue(
                    "multi_day_source_scope",
                    rid,
                    f"This source covers multiple days. Metadata assigns this record to {day_date}. Check all food entries and avoid counting repeated meals.",
                )
            )
    if len(members) > 1:
        content["issues"].append(
            issue(
                "same_day_reconciliation",
                unit_id,
                "Compare all source records for this participant and date. Resolve overlapping, duplicated or conflicting intake; do not silently count the same meal twice.",
            )
        )
    if pending:
        content["issues"].append(
            issue(
                "unresolved_day_membership",
                unit_id,
                "Potentially related source days have unresolved identity/date. The owner must resolve their grouping before approval.",
            )
        )
    snapshot["members"].sort(key=lambda m: (m["record_id"], m["day_id"]))
    content["issues"] = list({(i["code"], i["target"]): i for i in content["issues"]}.values())
    return RecordContent.model_validate(content).model_dump(mode="json"), snapshot


def sync_day_units(service):
    """Reconcile current source revisions. Old placements, drafts and releases are retained."""
    with service.factory.begin() as s:
        s.scalar(select(Project).where(Project.id == service.project_id).with_for_update())
        marker = "day-review-v1:" + service.project_id
        if not s.get(Audit, marker):
            legacy_records = list(s.scalars(select(Record).where(Record.project_id == service.project_id)))
            for r in legacy_records:
                if r.approved_id:
                    r.approved_id = None
                    if r.state == "approved":
                        r.state = "pending_review"
            for task in s.scalars(
                select(ReviewTask).where(ReviewTask.record_pk.in_([r.id for r in legacy_records]))
            ):
                if task.status not in {"stale", "cancelled", "returned"}:
                    task.status = "stale"
            s.add(
                Audit(
                    id=marker,
                    project_id=service.project_id,
                    actor_id="system",
                    action="day_review_migration",
                    target_id=service.project_id,
                    detail={"scope": "participant_date", "legacy_history_preserved": True},
                )
            )
        groups = defaultdict(list)
        unresolved = []
        for record in s.scalars(
            select(Record).where(Record.project_id == service.project_id).order_by(Record.record_id)
        ):
            if record.state == "excluded":
                continue
            source = current_snapshot(s, record)
            candidate = s.scalar(
                select(Candidate)
                .where(Candidate.record_pk == record.id, Candidate.input_version == record.input_version)
                .order_by(Candidate.created_at.desc(), Candidate.id.desc())
            )
            key = candidate.id if candidate else ""
            days = candidate.content.get("days", []) if candidate else []
            days = days or [{"day_id": "untranscribed", "date": {}}]
            extra_days = list(
                s.scalars(
                    select(DayPlacement).where(
                        DayPlacement.record_pk == record.id,
                        DayPlacement.input_version == record.input_version,
                        DayPlacement.candidate_key == key,
                        DayPlacement.day_id.startswith("manual:"),
                    )
                )
            )
            days = [*days, *[{"day_id": p.day_id, "date": {}} for p in extra_days]]
            source["day_count"] = len(days)
            for day in days:
                p = s.scalar(
                    select(DayPlacement).where(
                        DayPlacement.record_pk == record.id,
                        DayPlacement.input_version == record.input_version,
                        DayPlacement.candidate_key == key,
                        DayPlacement.day_id == day["day_id"],
                    )
                )
                if not p:
                    proposed, sid, day_date = proposal(record, candidate, day, source)
                    p = DayPlacement(
                        project_id=service.project_id,
                        record_pk=record.id,
                        input_version=record.input_version,
                        candidate_key=key,
                        day_id=day["day_id"],
                        subject_id=sid,
                        date=day_date,
                        proposed=proposed,
                        status="needs_confirmation" if proposed["problems"] else "consistent_sources",
                    )
                    s.add(p)
                    s.flush()
                # Reconcile existing automatic placements when adopting metadata authority.
                # Explicit additional days are retained as documented owner exceptions.
                if not p.day_id.startswith("manual:"):
                    proposed, sid, day_date = proposal(record, candidate, day, source)
                    if not proposed["problems"]:
                        desired = (sid, day_date, "metadata_assigned")
                        if (p.subject_id, p.date, p.status) != desired:
                            before = {"subject_id": p.subject_id, "date": p.date, "status": p.status}
                            p.subject_id, p.date, p.status = desired
                            p.proposed = proposed
                            p.reason = "Participant and intake date taken from authoritative metadata."
                            p.confirmed_by = None
                            service.audit(
                                s,
                                SimpleNamespace(id="system"),
                                "metadata_grouping_applied",
                                p.id,
                                {"before": before, "subject_id": sid, "date": day_date},
                            )
                            s.flush()
                item = (p, record, candidate, source)
                if p.status == "needs_confirmation":
                    unresolved.append(item)
                else:
                    groups[(p.subject_id, p.date)].append(item)
        active = set()
        for (sid, day_date), members in sorted(groups.items()):
            unit_id = "day-" + digest([service.project_id, sid, day_date])[:24]
            active.add(unit_id)
            pending = []
            for p, r, c, source in unresolved:
                subjects = {p.proposed["metadata_subject"], p.proposed["source_subject"]} - {None}
                dates = {p.proposed["metadata_date"], p.proposed["source_date"]} - {None}
                if sid in subjects and (not dates or day_date in dates):
                    pending.append(
                        {
                            "placement_id": p.id,
                            "revision": p.revision,
                            "record_id": r.record_id,
                            "day_id": p.day_id,
                        }
                    )
            content, snapshot = project_day(unit_id, sid, day_date, members, pending)
            policy = service.policy(s)
            fingerprint = digest({"snapshot": snapshot, "policy_id": policy.id if policy else None})
            unit = s.get(DayReviewUnit, unit_id)
            if not unit:
                unit = DayReviewUnit(
                    id=unit_id, project_id=service.project_id, subject_id=sid, date=day_date, input_version=0
                )
                s.add(unit)
                s.flush()
            unit.active_run_id = next((r.active_run_id for _, r, _, _ in members if r.active_run_id), None)
            if unit.fingerprint != fingerprint or unit.state == "retired":
                unit.input_version += 1
                unit.fingerprint = fingerprint
                unit.content = content
                unit.snapshot = snapshot
                unit.required_reviews = max(r.required_reviews for _, r, _, _ in members)
                unit.approved_id = None
                if unit.state != "excluded":
                    unit.state = "pending_review"
                for task in s.scalars(select(DayReviewTask).where(DayReviewTask.record_pk == unit.id)):
                    if task.status not in {"stale", "cancelled", "returned"}:
                        task.status = "stale"
                service.audit(
                    s,
                    SimpleNamespace(id="system"),
                    "day_membership_updated",
                    unit.id,
                    {
                        "version": unit.input_version,
                        "record_ids": sorted({r.record_id for _, r, _, _ in members}),
                    },
                )
        for unit in s.scalars(select(DayReviewUnit).where(DayReviewUnit.project_id == service.project_id)):
            if unit.id not in active and unit.state != "retired":
                unit.input_version += 1
                unit.state = "retired"
                unit.approved_id = None
                for task in s.scalars(select(DayReviewTask).where(DayReviewTask.record_pk == unit.id)):
                    if task.status not in {"stale", "cancelled", "returned"}:
                        task.status = "stale"
        return [(p.id, r.record_id) for p, r, _, _ in unresolved]
