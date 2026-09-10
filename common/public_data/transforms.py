"""Record transforms for DingTalk sync sheets.

Transforms reshape raw DingTalk records between reading and persisting,
without altering the source schema validation.
"""

from datetime import date


def melt_records(records, transform):
    """Unpivot wide date-column records into tall (one-row-per-date) records.

    Each source record with N date columns becomes up to N output records.
    Records with a ``None`` value for the date column are skipped.

    Parameters
    ----------
    records : list[dict]
        Raw DingTalk records (``{"id": ..., "fields": {...}}``).
    transform : dict
        ``{"type": "melt", "year": 2026, "month": 9,
           "date_columns": {"1日": 1, "2日": 2, ...},
           "value_column": "sales_amount",
           "inject": {"region": "杭州"}}``
    """
    year = transform["year"]
    month = transform["month"]
    date_columns = transform["date_columns"]
    value_column = transform["value_column"]
    inject_values = transform.get("inject", {})

    result = []
    for record in records:
        fields = record["fields"]
        base = {k: v for k, v in fields.items() if k not in date_columns}
        base.update(inject_values)

        for source_name, day in date_columns.items():
            value = fields.get(source_name)
            if value is None:
                continue
            new_fields = dict(base)
            new_fields["business_date"] = date(year, month, day)
            new_fields[value_column] = value
            result.append({
                "id": f"{record['id']}_{day:02d}",
                "fields": new_fields,
            })
    return result


def inject_records(records, transform):
    """Add static column values to every record.

    Used for fan-in patterns where multiple sheets write to the same target
    table and need a discriminator column (e.g. ``channel``).

    Parameters
    ----------
    records : list[dict]
        Raw DingTalk records.
    transform : dict
        ``{"type": "inject", "values": {"channel": "天猫"}}``
    """
    values = transform["values"]
    result = []
    for record in records:
        new_fields = dict(record["fields"])
        new_fields.update(values)
        result.append({"id": record["id"], "fields": new_fields})
    return result


def apply_transform(records, transform):
    """Dispatch to the correct transform function by type."""
    kind = transform["type"]
    if kind == "melt":
        return melt_records(records, transform)
    if kind == "inject":
        return inject_records(records, transform)
    raise ValueError(f"unknown transform type: {kind}")
