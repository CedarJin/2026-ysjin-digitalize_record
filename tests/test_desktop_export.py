"""Check export fidelity across records, shared events and multiple evidence references."""

import copy
import csv
import io
import json
import zipfile

from openpyxl import load_workbook
from test_extraction import sample

from food_record_digitizer.desktop_export import export_archive
from food_record_digitizer.schemas import RecordContent


def test_export_json_excel_csv_and_dictionary_agree():
    records = []
    for index in range(2):
        content = RecordContent.model_validate(sample()).model_dump(mode="json")
        event = content["events"][0]
        event["time"].update(normalized="08:30", raw="8.30", missing_reason=None)
        event["meal_label"].update(normalized=None, raw=None, missing_reason="not_recorded")
        item = copy.deepcopy(content["items"][0])
        item["item_id"] = "i2"
        item["description"].update(normalized="milk", raw="Milk")
        content["items"].append(item)
        # Two foods share an event; each has multiple source references.
        for item in content["items"]:
            item["evidence"] *= 3
        records.append(
            dict(
                record_id=f"R00{index + 1}",
                subject_id=f"P{index}",
                date="2026-10-20" if index == 0 else None,
                status="manual_draft",
                content=content,
            )
        )
    with zipfile.ZipFile(io.BytesIO(export_archive(records))) as archive:
        assert json.loads(archive.read("records.json")) == records
        book = load_workbook(io.BytesIO(archive.read("records.xlsx")), data_only=True)

        def rows(name):
            values = list(book[name].values)
            return [dict(zip(values[0], row)) for row in values[1:]]

        foods = rows("Food items")
        assert len(foods) == 4
        assert len(rows("Events")) == 2
        assert len(rows("Records")) == 2
        for record in records:
            actual = [row for row in foods if row["Record ID"] == record["record_id"]]
            assert [row["Item Id"] for row in actual] == [
                item["item_id"] for item in record["content"]["items"]
            ]
            assert [row["Description"] for row in actual] == [
                item["description"]["normalized"] for item in record["content"]["items"]
            ]
            assert all(
                row["Participant"] == record["subject_id"] and row["Metadata date"] == record["date"]
                for row in actual
            )
            assert all(
                row["Time"] == "08:30"
                and row["Meal Label"] is None
                and row["Meal Label missing reason"] == "not_recorded"
                for row in actual
            )
        assert len([row for row in rows("Evidence") if row["Field"] == "item"]) == 12
        dictionary = {(row["Table"], row["Column"]): row["Meaning"] for row in rows("Data dictionary")}
        for name in ("Records", "Food items", "Events", "Days", "Sources", "Evidence", "Issues"):
            assert all(dictionary[(name, cell.value)] for cell in book[name][1])
            csv_rows = list(
                csv.DictReader(io.StringIO(archive.read(name.lower().replace(" ", "_") + ".csv").decode()))
            )
            assert csv_rows == [
                {key: "" if value is None else str(value) for key, value in row.items()} for row in rows(name)
            ]
