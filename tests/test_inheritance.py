import pytest
from test_extraction import sample

from food_record_digitizer.schemas import RecordContent
from food_record_digitizer.validation import validate_content


@pytest.mark.parametrize("prefix", ["", "R001::"])
@pytest.mark.parametrize(
    "reference,valid",
    [
        ("i1", True),
        ("i1.amount", True),
        ("e1.place", True),
        ("d1.date", True),
        ("i1.missing", False),
        ("i1.item_id", False),
        ("missing.amount", False),
        ("i1.amount.normalized", False),
    ],
)
def test_inheritance_entity_and_field_references(prefix, reference, valid):
    raw = sample()
    raw["days"][0]["day_id"] = prefix + "d1"
    raw["events"][0].update(event_id=prefix + "e1", day_id=prefix + "d1")
    raw["items"][0].update(item_id=prefix + "i1", event_id=prefix + "e1")
    raw["items"][0]["unit"] = {"normalized": "cup", "inherited_from": prefix + reference}
    content = RecordContent.model_validate(raw)
    snapshot = {"documents": {"R001": {"page_count": 1}}, "metadata": {}}
    issues = validate_content(content, snapshot)
    assert any(i.code == "invalid_inheritance" for i in issues) is not valid
