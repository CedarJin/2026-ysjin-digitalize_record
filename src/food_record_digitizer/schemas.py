"""Versioned exchange schemas. Unknown is distinct from zero and empty text."""

from __future__ import annotations

import re
from datetime import date as Date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MANUAL_DRAFT_METHODS = {"manual_entry", "manual_attachment_update", "manual_entry_required"}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Metadata(Model):
    record_id: str = Field(min_length=1, max_length=128)
    subject_id: str | None = None
    data_type: str | None = None
    date: Date | str | None = None
    timepoint: str | None = None
    flag: str | None = None

    @model_validator(mode="after")
    def identifier(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", self.record_id):
            raise ValueError("record_id must use letters, digits, underscores or hyphens")
        return self


class Evidence(Model):
    source_record_id: str
    page_number: int = Field(ge=1)
    region: str = Field(min_length=1, description="Human-readable row/region locator")
    bbox: list[float] | None = None
    transcription_raw: str = ""

    @model_validator(mode="after")
    def coordinates(self):
        if self.bbox is not None:
            if len(self.bbox) != 4 or not all(0 <= n <= 1 for n in self.bbox):
                raise ValueError("bbox must contain four normalized coordinates")
            x1, y1, x2, y2 = self.bbox
            if x1 >= x2 or y1 >= y2:
                raise ValueError("bbox must have positive area")
        return self


class MetadataReference(Model):
    record_id: str
    field: Literal["subject_id", "date"]


class Value(Model):
    raw: str | None = None
    normalized: str | None = None
    missing_reason: Literal["not_recorded", "illegible", "ambiguous", "not_applicable"] | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    inherited_from: str | None = None
    metadata_reference: MetadataReference | None = None

    @model_validator(mode="after")
    def unknown(self):
        if self.normalized is None and self.missing_reason is None:
            raise ValueError("unknown values require a missing_reason")
        if self.normalized is not None and self.missing_reason is not None:
            raise ValueError("known values cannot have missing_reason")
        if self.normalized is not None and not self.normalized.strip():
            raise ValueError("use null and missing_reason instead of empty text")
        if (
            self.normalized is not None
            and not self.evidence
            and not self.inherited_from
            and not self.metadata_reference
        ):
            raise ValueError("known values require source evidence or an inheritance reference")
        return self


def unknown() -> Value:
    return Value(missing_reason="not_recorded")


class FoodItem(Model):
    item_id: str = Field(min_length=1)
    event_id: str
    description: Value = Field(default_factory=unknown)
    amount: Value = Field(default_factory=unknown)
    unit: Value = Field(default_factory=unknown)
    brand: Value = Field(default_factory=unknown)
    preparation: Value = Field(default_factory=unknown)
    measurement_method: Value = Field(default_factory=unknown)
    raw_or_cooked: Value = Field(default_factory=unknown)
    quantity: float | None = Field(default=None, ge=0)
    quantity_max: float | None = Field(default=None, ge=0)
    evidence: list[Evidence] = Field(min_length=1)
    notes: str = ""

    @model_validator(mode="after")
    def quantity_range(self):
        if self.quantity is not None and self.amount.normalized is None:
            raise ValueError("numeric quantity requires a supported amount")
        if self.quantity_max is not None and (self.quantity is None or self.quantity_max < self.quantity):
            raise ValueError("invalid quantity range")
        return self


class RecordDay(Model):
    day_id: str
    date: Value = Field(default_factory=unknown)
    date_needs_confirmation: bool = False


class EatingEvent(Model):
    event_id: str
    day_id: str
    time: Value = Field(default_factory=unknown)
    meal_label: Value = Field(default_factory=unknown)
    place: Value = Field(default_factory=unknown)


class PageCoverage(Model):
    source_record_id: str
    page_number: int = Field(ge=1)
    classification: Literal["food_record", "clarification", "recipe", "blank", "other"]
    regions: list[str] = Field(default_factory=list)
    notes: str = ""
    source_incomplete: bool = False
    reviewed: bool = False


class Issue(Model):
    code: str
    severity: Literal["warning", "blocking"]
    target: str
    message: str
    resolution: str | None = None


class RecordContent(Model):
    schema_version: Literal["1.0"] = "1.0"
    record_id: str
    subject: Value = Field(default_factory=unknown)
    days: list[RecordDay] = Field(default_factory=list)
    events: list[EatingEvent] = Field(default_factory=list)
    items: list[FoodItem] = Field(default_factory=list)
    pages: list[PageCoverage] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def references(self):
        for name, values in [
            ("days", [v.day_id for v in self.days]),
            ("events", [v.event_id for v in self.events]),
            ("items", [v.item_id for v in self.items]),
            ("pages", [(v.source_record_id, v.page_number) for v in self.pages]),
        ]:
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate identifiers in {name}")
        days = {v.day_id for v in self.days}
        events = {v.event_id for v in self.events}
        if any(v.day_id not in days for v in self.events):
            raise ValueError("event references missing day")
        if any(v.event_id not in events for v in self.items):
            raise ValueError("food item references missing event")
        return self
