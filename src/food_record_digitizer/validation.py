"""Deterministic checks against the frozen source manifest, independent of AI."""

from __future__ import annotations

from .schemas import Issue, RecordContent

FLAG_RULES = {
    "don't use": "exclusion_pending",
    "inconsistent pat": "diet_type_conflict",
    "multiple days on the same page": "multiple_days",
    "3 days on same page": "multiple_days",
    "only 1 day provided, the participant repeated it for 3 days": "repeated_day",
    "incorrect format": "nonstandard_form",
    "is a recipe": "recipe",
    "has recipe as image": "recipe_attachment",
    "doesn't match her day 90 or day180": "timepoint_conflict",
    "2 pat diet detected": "diet_type_conflict",
    "used as hab": "diet_type_conflict",
}


# These checks are derived from the current content and source manifest. They must
# disappear when corrected, rather than surviving as historical "active" issues.
VALIDATION_CODES = {
    "page_coverage_mismatch",
    "missing_main",
    "missing_source",
    "unverified_subject",
    "subject_conflict",
    "invalid_metadata_reference",
    "invalid_evidence",
    "invalid_inheritance",
    "inherited_value",
    "invalid_metadata_date",
    "date_conflict",
    "invalid_date",
    "attachment_metadata_conflict",
    "source_incomplete",
    "no_items",
    "attachment_only_item",
    "unresolved_day_membership",
    "unclassified_flag",
    *FLAG_RULES.values(),
}
APPROVAL_BLOCKING_CODES = {
    "page_coverage_mismatch",
    "invalid_metadata_reference",
    "invalid_evidence",
    "invalid_inheritance",
    "missing_main",
    "missing_source",
    "unverified_subject",
    "invalid_date",
    "unresolved_day_membership",
}


def refresh_issues(content: RecordContent, snapshot: dict, baseline: dict | None = None):
    """Refresh current checks, retaining optional notes and original machine reminders."""
    existing = {(i.code, i.target): i for i in content.issues}
    issues = {key: i for key, i in existing.items() if i.code not in VALIDATION_CODES}
    for raw in (baseline or {}).get("issues", []):
        if raw["code"] not in VALIDATION_CODES:
            issues.setdefault((raw["code"], raw["target"]), Issue.model_validate(raw))
    for item in validate_content(content, snapshot):
        previous = existing.get((item.code, item.target))
        if previous:
            item.resolution = previous.resolution
        issues[(item.code, item.target)] = item
    content.issues = list(issues.values())
    return content


def flag_codes(flag: str | None) -> list[str]:
    if not flag:
        return []
    codes = sorted({code for text, code in FLAG_RULES.items() if text in flag.lower()})
    return codes or ["unclassified_flag"]


def validate_content(content: RecordContent, snapshot: dict) -> list[Issue]:
    issues = []

    def add(code, target, message, severity="blocking"):
        issues.append(Issue(code=code, target=target, message=message, severity=severity))

    docs = snapshot["documents"]
    meta = snapshot.get("review_unit") or snapshot["metadata"].get(content.record_id, {})
    expected = {(sid, n) for sid, doc in docs.items() for n in range(1, doc["page_count"] + 1)}
    actual = {(p.source_record_id, p.page_number) for p in content.pages}
    if actual != expected:
        add(
            "page_coverage_mismatch",
            "pages",
            "Every input page must appear exactly once in the coverage list.",
        )
    if not snapshot.get("review_unit") and content.record_id not in docs:
        add("missing_main", "record_id", "The main source document is missing.")
    if snapshot.get("review_unit"):
        if snapshot.get("pending_placements"):
            add(
                "unresolved_day_membership",
                content.record_id,
                "Resolve related source day grouping before approval.",
            )
        for member in snapshot.get("members", []):
            if member["record_id"] not in docs:
                add("missing_main", member["record_id"], "The main source document is missing.")
        for sid, doc in docs.items():
            if doc.get("error"):
                add("missing_source", sid, "This source cannot be read.")
    for source_id in sorted(set(snapshot["metadata"]) - set(docs)):
        add(
            "missing_source",
            source_id,
            "Metadata lists this source, but its scan has not been supplied. Review coverage is incomplete.",
        )
    if not content.subject.normalized:
        add("unverified_subject", "subject", "Subject identity needs human confirmation.")
    elif meta.get("subject_id") and content.subject.normalized.casefold() != meta["subject_id"].casefold():
        add("subject_conflict", "subject", "The page subject differs from metadata.")
    # Find all evidence, including per-field references.
    payload = content.model_dump(mode="json")
    entities = {
        entity[key]: entity
        for group, key in [("items", "item_id"), ("events", "event_id"), ("days", "day_id")]
        for entity in payload[group]
    }

    def valid_inheritance(reference):
        if reference in entities:
            return True
        entity_id, separator, field = reference.rpartition(".")
        target = entities.get(entity_id, {}).get(field)
        # AI output may identify the exact source field (e.g. item-1.amount).
        # Accept only an existing Value field, never arbitrary paths or IDs.
        return bool(
            separator and isinstance(target, dict) and "normalized" in target and "evidence" in target
        )

    def walk(value, path):
        if isinstance(value, dict):
            if {"source_record_id", "page_number", "region"} <= value.keys():
                if (value["source_record_id"], value["page_number"]) not in expected:
                    add("invalid_evidence", path, "Evidence references a missing source page.")
            if ref := value.get("metadata_reference"):
                expected_field = (
                    "subject_id"
                    if path == "record.subject"
                    else "date"
                    if path == "record.days[0].date"
                    else None
                )
                row = snapshot["metadata"].get(ref["record_id"], {})
                if (
                    ref["field"] != expected_field
                    or not row.get(ref["field"])
                    or str(row[ref["field"]]).strip() != value.get("normalized")
                ):
                    add(
                        "invalid_metadata_reference",
                        path,
                        "Value must match its authoritative metadata field.",
                    )
            if value.get("inherited_from"):
                # Keep both entity-level and explicit field-level provenance.
                if not valid_inheritance(value["inherited_from"]):
                    add(
                        "invalid_inheritance",
                        path,
                        "Inherited value references an unknown entity or value field.",
                    )
                else:
                    add(
                        "inherited_value",
                        path,
                        "Confirm that this value applies across the referenced rows.",
                        "warning",
                    )
            for key, child in value.items():
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, f"{path}[{i}]")

    walk(payload, "record")
    metadata_date = meta.get("date")
    if metadata_date:
        from datetime import date

        try:
            date.fromisoformat(metadata_date)
        except ValueError:
            add(
                "invalid_metadata_date",
                "metadata.date",
                "Metadata date is not an ISO date; preserve its raw value.",
            )
            metadata_date = None
    for day in content.days:
        if day.date.normalized and metadata_date and day.date.normalized != metadata_date:
            add("date_conflict", day.day_id, "Page date differs from metadata; check multi-day records.")
        if day.date.normalized:
            from datetime import date

            try:
                date.fromisoformat(day.date.normalized)
            except ValueError:
                add("invalid_date", day.day_id, "Normalized date must be an ISO date.")
    for sid, row in snapshot["metadata"].items():
        for code in flag_codes(row.get("flag")):
            add(code, sid, "Review the original metadata flag before approval.")
        attachment_meta = meta
        is_attachment = sid != content.record_id
        if snapshot.get("review_unit"):
            # Compare an attachment with its own original main record, not the synthetic day ID.
            parent = next(
                (m["record_id"] for m in snapshot["members"] if sid.startswith(m["record_id"] + "-A")), None
            )
            is_attachment = parent is not None
            attachment_meta = snapshot["metadata"].get(parent, {})
        if is_attachment:
            for field in ("subject_id", "date", "data_type", "timepoint"):
                if row.get(field) and attachment_meta.get(field) and row[field] != attachment_meta[field]:
                    add(
                        "attachment_metadata_conflict",
                        f"{sid}.{field}",
                        "Attachment and main metadata disagree.",
                    )
    for page in content.pages:
        if page.source_incomplete:
            add(
                "source_incomplete",
                f"{page.source_record_id}/{page.page_number}",
                "This page is currently flagged as possibly incomplete. Check the source; clear the flag if the page is complete, or obtain missing material when needed.",
            )
    if not content.items:
        add("no_items", "items", "Confirm that no food or beverage was omitted.")
    for item in content.items:
        if not any(docs.get(e.source_record_id, {}).get("role") == "main" for e in item.evidence):
            add(
                "attachment_only_item",
                item.item_id,
                "An attachment-only food must be confirmed as actual intake.",
            )
    return issues
