import copy

import pytest
from pydantic import ValidationError

from food_record_digitizer.schemas import FoodItem


def row():
    return dict(
        item_id="food1",
        event_id="event1",
        food_name=dict(raw="WATER", normalized="Water"),
        amount=dict(raw="16", normalized="16"),
        evidence=[
            dict(source_record_id="R1", page_number=1, region="Morning row", transcription_raw="16 oz WATER")
        ],
    )


def test_supported_fields_inherit_without_mutating_response():
    data = row()
    original = copy.deepcopy(data)
    item = FoodItem.model_validate(data)
    assert item.food_name.evidence == item.evidence
    assert item.amount.evidence == item.evidence
    assert data == original
    assert FoodItem.model_validate(item.model_dump()) == item


@pytest.mark.parametrize("raw", ["6", "Milk", None])
def test_unsupported_fields_still_fail(raw):
    data = row()
    data["amount"] = dict(raw=raw, normalized="6")
    with pytest.raises(ValidationError):
        FoodItem.model_validate(data)


def test_ingredient_does_not_borrow_parent_evidence():
    data = row()
    data["ingredients"] = [
        dict(
            item_id="ingredient1",
            food_name=dict(raw="WATER", normalized="Water"),
            evidence=[
                dict(source_record_id="R1", page_number=2, region="Recipe row", transcription_raw="Flour")
            ],
        )
    ]
    with pytest.raises(ValidationError):
        FoodItem.model_validate(data)


def test_explicit_evidence_is_preserved_and_validated():
    data = row()
    data["food_name"]["evidence"] = [dict(source_record_id="R1", page_number=2, region="Other row")]
    assert FoodItem.model_validate(data).food_name.evidence[0].page_number == 2
    data["food_name"]["evidence"][0]["page_number"] = 0
    with pytest.raises(ValidationError):
        FoodItem.model_validate(data)
