"""Record transforms for DingTalk sync sheets.

Transforms reshape raw DingTalk records between reading and persisting,
without altering the source schema validation.
"""

from datetime import date


def _coerce_number(value):
    """日格值 → float；空/非数字 → None（按未填处理，与 melt 的 None-skip 同语义）。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


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
           "value_semantics": "daily" | "cumulative",
           "inject": {"region": "杭州"}}``

    ``value_semantics``（缺省 ``"daily"``，行为与旧版逐字一致）：
    * ``daily``——每格即当日销售额；
    * ``cumulative``——格内是「累计到当天」的数（填表人习惯），输出按
      当日 = 本格 − 上一有值格（日期序），**首个有值格原样保留**（期初）。
      差值模式强制数值化：空串/非数字格按未填跳过；累计回落产生负值
      （冲销/退货），原样输出不截断。
    """
    year = transform["year"]
    month = transform["month"]
    date_columns = transform["date_columns"]
    value_column = transform["value_column"]
    inject_values = transform.get("inject", {})
    semantics = transform.get("value_semantics", "daily")

    result = []
    for record in records:
        fields = record["fields"]
        base = {k: v for k, v in fields.items() if k not in date_columns}
        base.update(inject_values)

        if semantics == "cumulative":
            filled = []
            for source_name, day in date_columns.items():
                value = _coerce_number(fields.get(source_name))
                if value is None:
                    continue
                filled.append((day, value))
            filled.sort(key=lambda item: item[0])
            day_values = []
            previous = None
            for day, value in filled:
                day_values.append((day, value if previous is None else value - previous))
                previous = value
        else:
            day_values = [
                (day, fields.get(source_name))
                for source_name, day in date_columns.items()
                if fields.get(source_name) is not None
            ]

        for day, value in day_values:
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
