# ruff: noqa: F811
import copy
import csv
import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from test_day_review import day_review  # noqa: F401
from test_extraction import sample
from test_ingest import ws  # noqa: F401

from food_record_digitizer import db
from food_record_digitizer.desktop_server import study_app
from food_record_digitizer.offline import OfflineManager
from food_record_digitizer.offline_reviewer import OfflineReviewer
from food_record_digitizer.research_export import COLUMNS, research_csv
from food_record_digitizer.review import ReviewError
from food_record_digitizer.schemas import RecordContent
from food_record_digitizer.validation import validate_content


def with_recipe(raw):
    data = RecordContent.model_validate(raw).model_dump(mode="json")
    item = data["items"][0]
    ev = item["evidence"]

    def val(text):
        return dict(normalized=text, evidence=copy.deepcopy(ev))

    item.update(
        food_name=val("Banana bread"),
        description=val("Homemade, with walnuts"),
        amount=val("1"),
        unit=val("slice"),
        recipe_yield=val("1 loaf"),
        ingredient_basis="whole_recipe",
        recipe=val("Mix and bake"),
    )
    item["ingredients"] = [
        dict(
            item_id="flour",
            food_name=val("Flour"),
            description=val("Whole wheat"),
            amount=val("200"),
            unit=val("g"),
            evidence=copy.deepcopy(ev),
        )
    ]
    return RecordContent.model_validate(data).model_dump(mode="json")


def test_recipe_csv_has_one_consumed_food_and_no_internal_columns():
    content = with_recipe(sample())
    row = list(
        csv.DictReader(
            io.StringIO(
                research_csv([dict(record_id="R001", subject_id="P1", content=content)], "Study1").decode(
                    "utf-8-sig"
                )
            )
        )
    )[0]
    assert list(row) == COLUMNS
    assert row["Study ID"] == "Study1" and row["Participant ID"] == "P1"
    assert row["Food"] == "Banana bread" and row["Description"] == "Homemade, with walnuts"
    assert (row["Amount"], row["Unit"]) == ("1", "slice")
    assert "whole recipe" in row["Recipe"] and "Flour (200 g; Whole wheat)" in row["Recipe"]
    assert "Recipe yield: 1 loaf" in row["Recipe"]
    assert "Event ID" not in row


def test_recipe_ids_and_nested_evidence_are_checked():
    content = with_recipe(sample())
    ingredient = content["items"][0]["ingredients"][0]
    ingredient["item_id"] = content["items"][0]["item_id"]
    with pytest.raises(ValidationError, match="duplicate identifiers"):
        RecordContent.model_validate(content)
    ingredient["item_id"] = "flour"
    ingredient["evidence"][0]["source_record_id"] = "missing"
    snapshot = dict(documents={"R001": {"page_count": 1, "role": "main"}}, metadata={})
    issues = validate_content(RecordContent.model_validate(content), snapshot)
    assert any(i.code == "invalid_evidence" and "ingredients" in i.target for i in issues)


def test_legacy_export_does_not_invent_food_name_or_recipe():
    content = sample()
    data = research_csv([dict(record_id="R001", content=content)], "S").decode("utf-8-sig")
    row = list(csv.DictReader(io.StringIO(data)))[0]
    assert row["Food"] == content["items"][0]["description"]["normalized"]
    assert row["Recipe"] == row["Description"] == ""
    content["items"][0]["description"]["normalized"] = "=UNTRUSTED()"
    assert "'=UNTRUSTED()" in research_csv([dict(record_id="R001", content=content)]).decode("utf-8-sig")


def test_recipe_projection_offline_roundtrip_and_csv_endpoint(day_review, tmp_path):
    svc, owner, reviewer, _, _ = day_review
    with svc.factory.begin() as session:
        candidate = session.scalar(select(db.Candidate))
        candidate.content = with_recipe(candidate.content)
    rid = svc.queue(owner)[0]["record_id"]
    detail = svc.detail(owner, rid)
    item = detail["content"]["items"][0]
    assert item["ingredients"][0]["item_id"] == "R001::flour"
    assert len(detail["content"]["items"]) == 1
    manager = OfflineManager(svc.workspace)
    package = manager.export(owner, reviewer, [rid])[0]
    local = OfflineReviewer(tmp_path / "offline", Path(package["path"]).read_bytes())
    detail = local.detail(rid)
    data = detail["content"]
    data["items"][0]["ingredients"][0]["amount"]["normalized"] = "250"
    for page in data["pages"]:
        page.update(reviewed=True, regions=["All entries"])
    data["items"][0]["ingredient_relationship_needs_confirmation"] = True
    saved = local.save(detail["task"]["id"], detail["task"]["revision"], data)
    with pytest.raises(ReviewError, match="ingredient"):
        local.save(detail["task"]["id"], saved["revision"], None, submit=True)
    data["items"][0]["ingredient_relationship_needs_confirmation"] = False
    saved = local.save(detail["task"]["id"], saved["revision"], data)
    local.save(detail["task"]["id"], saved["revision"], None, submit=True)
    receipt = manager.import_result(owner, local.export_result())
    assert all(t["status"] == "accepted" for t in receipt["tasks"])
    svc.approve(owner, rid)
    release_id = svc.release(owner, [rid])
    app = study_app(svc.workspace, "test-capability")
    with TestClient(app, headers={"x-desktop-capability": "test-capability"}) as client:
        response = client.get(f"/desktop/export/release/{release_id}?format=csv")
        assert response.status_code == 200 and "text/csv" in response.headers["content-type"]
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert len(rows) == 1 and "250 g" in rows[0]["Recipe"]
        assert rows[0]["Study ID"] == svc.project_id
        response = client.get(f"/desktop/export/release/{release_id}")
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert archive.read("food_records.csv").decode("utf-8-sig").count("Banana bread") == 1
