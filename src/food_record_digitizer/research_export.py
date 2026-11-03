"""Research-facing CSV: one row per consumed food, never per recipe ingredient."""

import csv
import io

COLUMNS = [
    "Study ID",
    "Participant ID",
    "Date",
    "Time",
    "Meal label",
    "Food",
    "Description",
    "Amount",
    "Unit",
    "Recipe",
    "Preparation method",
    "Place",
    "Brand",
    "Notes",
    "Source record",
]


def value(obj, key):
    # Normalized unknowns stay blank; illegible/ambiguous raw text is not a confirmed value.
    return (obj.get(key) or {}).get("normalized") or ""


def recipe_text(item):
    parts = []
    if text := value(item, "recipe"):
        parts.append(text)
    ingredients = []
    for ingredient in item.get("ingredients", []):
        name = value(ingredient, "food_name") or value(ingredient, "description") or "Unnamed ingredient"
        amount = " ".join(filter(None, [value(ingredient, "amount"), value(ingredient, "unit")]))
        detail = "; ".join(
            filter(
                None,
                [
                    amount,
                    value(ingredient, "description") if value(ingredient, "food_name") else "",
                    value(ingredient, "brand"),
                    value(ingredient, "preparation"),
                ],
            )
        )
        ingredients.append(name + (f" ({detail})" if detail else ""))
    if ingredients:
        basis = {"whole_recipe": "whole recipe", "consumed_portion": "consumed portion"}.get(
            item.get("ingredient_basis"), "quantity basis unknown"
        )
        parts.append(f"Ingredients [{basis}]: " + "; ".join(ingredients))
    if amount := value(item, "recipe_yield"):
        parts.append("Recipe yield: " + amount)
    return " | ".join(parts)


def source_ids(obj):
    if isinstance(obj, dict):
        if {"source_record_id", "page_number", "region"} <= obj.keys():
            yield obj["source_record_id"]
        for v in obj.values():
            yield from source_ids(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from source_ids(v)


def research_rows(records, study_id=""):
    rows = []
    for record in records:
        content = record["content"]
        events = {e["event_id"]: e for e in content["events"]}
        days = {d["day_id"]: d for d in content["days"]}
        for item in content["items"]:
            event = events.get(item["event_id"], {})
            day = days.get(event.get("day_id"), {})
            name = value(item, "food_name")
            sources = sorted(set(source_ids(item)))
            rows.append(
                dict(
                    zip(
                        COLUMNS,
                        [
                            study_id,
                            record.get("subject_id") or value(content, "subject"),
                            value(day, "date"),
                            value(event, "time"),
                            value(event, "meal_label"),
                            name or value(item, "description"),
                            value(item, "description") if name else "",
                            value(item, "amount"),
                            value(item, "unit"),
                            recipe_text(item),
                            value(item, "preparation"),
                            value(event, "place"),
                            value(item, "brand"),
                            item.get("notes", ""),
                            "; ".join(sources) or record["record_id"],
                        ],
                    )
                )
            )
    return sorted(rows, key=lambda r: (r["Participant ID"] or "", r["Date"] or "9999", r["Time"] or ""))


def research_csv(records, study_id=""):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    for row in research_rows(records, study_id):
        writer.writerow(
            {
                k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v
                for k, v in row.items()
            }
        )
    return stream.getvalue().encode("utf-8-sig")
