import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ManifestError(ValueError):
    pass


_ALLOWED_SOURCE_TYPES = frozenset({
    "text", "singleSelect", "number", "currency", "date", "user", "multipleSelect", "unidirectionalLink",
})

_ALLOWED_WDT_METHODS = frozenset({
    "sales.TradeQuery.queryWithDetail",
    "wms.stockin.Purchase.queryWithDetail",
    "wms.StockSpec.search2",
    "goods.Goods.queryWithSpec",
})

_KNOWN_TABLES = frozenset({
    "fin_store_commission",
    "fin_tax_declaration_2026",
    "fin_tax_sales_reconciliation_2026",
    "fin_tax_stamp_duty",
    "fin_tax_uninvoiced_sales_summary",
    "fin_tax_uninvoiced_sales",
    "fin_tax_prior_period_invoice",
    "fin_tax_pre_invoice",
    "fin_tax_input_invoice",
    "fin_ecommerce_prepayment_supplier_invoice",
    "fin_ecommerce_promotion_recharge_balance",
    "fin_ecommerce_platform_deposit",
    "fin_ecommerce_store_funds_balance",
    "fin_billion_subsidy_pdd",
    "fin_billion_subsidy_douyin",
    "fin_daily_funds",
    "fin_offline_receivables_aging",
    "fin_offline_deposit_other_receivables",
    "daily_report_offline",
    "channel_daily_sales",
    "wdt_records",
    "dingtalk_org_member",
})

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DOTTED_ID_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")


@dataclass(frozen=True)
class FieldMapping:
    source_name: str
    column: str
    source_type: str


@dataclass(frozen=True)
class DingTalkSheet:
    base_id: str
    sheet_id: str
    sheet_name: str
    dataset: str
    target_table: str
    max_pages: int
    fields: tuple[FieldMapping, ...]
    transform: dict | None = None


@dataclass(frozen=True)
class WdtDataset:
    dataset: str
    method: str
    target_table: str
    record_id_path: str
    page_size: int
    max_pages: int
    window_start: datetime
    window_end: datetime
    max_window_minutes: int
    params: dict[str, Any]
    time_boxed: bool = True


@dataclass(frozen=True)
class OrgDataset:
    """Contact-directory sync declaration (``dingtalk.org``).

    Unlike the AI-table sheets, the directory is a REST surface, so it is
    declared as its own section instead of being forced into
    ``dingtalk.bases[].sheets[]`` (same reasoning as the attendance
    decision in the spec).  The region->dept mapping is *not* part of the
    manifest -- it is business configuration and lives in the
    version-controlled org seed.
    """

    dataset: str
    target_table: str


@dataclass(frozen=True)
class SourceManifest:
    dingtalk_sheets: tuple[DingTalkSheet, ...]
    wdt_datasets: tuple[WdtDataset, ...]
    sha256: str
    dingtalk_org: OrgDataset | None = None


def _parse_utc(value: str, label: str) -> datetime:
    if not isinstance(value, str):
        raise ManifestError(f"{label} must be a string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ManifestError(f"{label} is not a valid timestamp: {value}") from exc
    if dt.tzinfo is not timezone.utc:
        raise ManifestError(f"{label} must be UTC (got {dt.tzinfo})")
    return dt


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise ManifestError(f"{label} must match [a-z][a-z0-9_]*: {value!r}")


def _validate_dotted_id(value: str, label: str) -> None:
    if not isinstance(value, str) or not _DOTTED_ID_RE.match(value):
        raise ManifestError(f"{label} must be a dotted identifier: {value!r}")


def _parse_transform(raw, prefix: str) -> dict | None:
    """Parse and validate the optional ``transform`` block on a sheet."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError(f"{prefix}.transform must be an object")

    kind = raw.get("type")
    if kind not in ("melt", "inject"):
        raise ManifestError(
            f"{prefix}.transform.type must be 'melt' or 'inject' (got {kind!r})"
        )

    if kind == "melt":
        year = raw.get("year")
        if not isinstance(year, int) or year < 2000:
            raise ManifestError(f"{prefix}.transform.year must be a valid year")
        month = raw.get("month")
        if not isinstance(month, int) or not (1 <= month <= 12):
            raise ManifestError(f"{prefix}.transform.month must be 1..12")
        date_columns = raw.get("date_columns")
        if not isinstance(date_columns, dict) or not date_columns:
            raise ManifestError(
                f"{prefix}.transform.date_columns must be a non-empty object"
            )
        for col_name, day in date_columns.items():
            if not isinstance(col_name, str) or not col_name:
                raise ManifestError(
                    f"{prefix}.transform.date_columns key must be non-empty"
                )
            if not isinstance(day, int) or not (1 <= day <= 31):
                raise ManifestError(
                    f"{prefix}.transform.date_columns[{col_name!r}] must be 1..31"
                )
        value_column = raw.get("value_column")
        if not isinstance(value_column, str) or not value_column:
            raise ManifestError(
                f"{prefix}.transform.value_column is required for melt"
            )

        result = {
            "type": "melt",
            "year": year,
            "month": month,
            "date_columns": dict(date_columns),
            "value_column": value_column,
        }

        inject = raw.get("inject")
        if inject is not None:
            if not isinstance(inject, dict) or not inject:
                raise ManifestError(
                    f"{prefix}.transform.inject must be a non-empty object"
                )
            for key in inject:
                if not isinstance(key, str) or not key:
                    raise ManifestError(
                        f"{prefix}.transform.inject key must be non-empty"
                    )
            result["inject"] = dict(inject)

        return result

    if kind == "inject":
        values = raw.get("values")
        if not isinstance(values, dict) or not values:
            raise ManifestError(
                f"{prefix}.transform.values must be a non-empty object"
            )
        for key, val in values.items():
            if not isinstance(key, str) or not key:
                raise ManifestError(
                    f"{prefix}.transform.values key must be non-empty"
                )
        return {"type": "inject", "values": dict(values)}

    return None


def _load_dingtalk_bases(bases: list, known_tables: frozenset) -> tuple[DingTalkSheet, ...]:
    sheets: list[DingTalkSheet] = []
    seen_datasets: set[str] = set()

    for base_index, base in enumerate(bases):
        if not isinstance(base, dict):
            raise ManifestError(f"dingtalk.bases[{base_index}] must be an object")
        base_id = base.get("base_id")
        if not isinstance(base_id, str) or not base_id:
            raise ManifestError(f"dingtalk.bases[{base_index}].base_id is required")

        raw_sheets = base.get("sheets")
        if not isinstance(raw_sheets, list):
            raise ManifestError(f"dingtalk.bases[{base_index}].sheets must be a list")

        seen_sheet_ids: set[str] = set()

        for sheet_index, raw in enumerate(raw_sheets):
            prefix = f"dingtalk.bases[{base_index}].sheets[{sheet_index}]"
            if not isinstance(raw, dict):
                raise ManifestError(f"{prefix} must be an object")

            sheet_id = raw.get("sheet_id")
            if not isinstance(sheet_id, str) or not sheet_id:
                raise ManifestError(f"{prefix}.sheet_id is required")
            if sheet_id in seen_sheet_ids:
                raise ManifestError(f"duplicate sheet_id: {sheet_id}")
            seen_sheet_ids.add(sheet_id)

            sheet_name = raw.get("sheet_name")
            if not isinstance(sheet_name, str) or not sheet_name:
                raise ManifestError(f"{prefix}.sheet_name is required")

            dataset = raw.get("dataset")
            if not isinstance(dataset, str) or not dataset:
                raise ManifestError(f"{prefix}.dataset is required")
            if dataset in seen_datasets:
                raise ManifestError(f"duplicate dataset: {dataset}")
            seen_datasets.add(dataset)

            target_table = raw.get("target_table")
            if not isinstance(target_table, str) or not target_table:
                raise ManifestError(f"{prefix}.target_table is required")
            if target_table not in known_tables:
                raise ManifestError(f"{prefix}.target_table is not registered: {target_table}")

            max_pages = raw.get("max_pages")
            if not isinstance(max_pages, int) or max_pages < 1:
                raise ManifestError(f"{prefix}.max_pages must be a positive integer")

            raw_mapping = raw.get("field_mapping")
            if not isinstance(raw_mapping, dict):
                raise ManifestError(f"{prefix}.field_mapping must be an object")

            fields: list[FieldMapping] = []
            seen_columns: set[str] = set()
            for source_name, mapping in raw_mapping.items():
                if not isinstance(source_name, str) or not source_name:
                    raise ManifestError(f"{prefix}.field_mapping key must be non-empty")
                if not isinstance(mapping, dict):
                    raise ManifestError(f"{prefix}.field_mapping[{source_name}] must be an object")
                column = mapping.get("column")
                if not isinstance(column, str) or not column:
                    raise ManifestError(f"{prefix}.field_mapping[{source_name}].column is required")
                if column in seen_columns:
                    raise ManifestError(f"duplicate column: {column}")
                seen_columns.add(column)
                source_type = mapping.get("source_type")
                if source_type not in _ALLOWED_SOURCE_TYPES:
                    raise ManifestError(
                        f"{prefix}.field_mapping[{source_name}].source_type must be one of "
                        f"{sorted(_ALLOWED_SOURCE_TYPES)}"
                    )
                fields.append(FieldMapping(source_name=source_name, column=column, source_type=source_type))

            transform = _parse_transform(raw.get("transform"), prefix)

            sheets.append(DingTalkSheet(
                base_id=base_id,
                sheet_id=sheet_id,
                sheet_name=sheet_name,
                dataset=dataset,
                target_table=target_table,
                max_pages=max_pages,
                fields=tuple(fields),
                transform=transform,
            ))

    return tuple(sheets)


def _load_dingtalk_org(raw) -> OrgDataset | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("dingtalk.org must be an object")
    dataset = raw.get("dataset")
    if not isinstance(dataset, str) or not dataset:
        raise ManifestError("dingtalk.org.dataset is required")
    _validate_identifier(dataset, "dingtalk.org.dataset")
    target_table = raw.get("target_table")
    if target_table != "dingtalk_org_member":
        raise ManifestError("dingtalk.org.target_table must be dingtalk_org_member")
    return OrgDataset(dataset=dataset, target_table=target_table)


def _load_wdt_datasets(datasets: list) -> tuple[WdtDataset, ...]:
    result: list[WdtDataset] = []
    seen_datasets: set[str] = set()
    method_windows: dict[str, list[tuple[datetime, datetime]]] = {}

    for index, raw in enumerate(datasets):
        prefix = f"wdt.datasets[{index}]"
        if not isinstance(raw, dict):
            raise ManifestError(f"{prefix} must be an object")

        dataset = raw.get("dataset")
        if not isinstance(dataset, str) or not dataset:
            raise ManifestError(f"{prefix}.dataset is required")
        if dataset in seen_datasets:
            raise ManifestError(f"duplicate wdt dataset: {dataset}")
        seen_datasets.add(dataset)

        method = raw.get("method")
        if method not in _ALLOWED_WDT_METHODS:
            raise ManifestError(f"{prefix}.method is not allowed: {method}")

        target_table = raw.get("target_table")
        if not isinstance(target_table, str) or not target_table:
            raise ManifestError(f"{prefix}.target_table is required")
        if target_table != "wdt_records":
            raise ManifestError(f"{prefix}.target_table must be wdt_records")

        record_id_path = raw.get("record_id_path")
        if not isinstance(record_id_path, str) or not record_id_path:
            raise ManifestError(f"{prefix}.record_id_path is required")
        for _id_part in record_id_path.split(","):
            _validate_dotted_id(_id_part.strip(), f"{prefix}.record_id_path")

        page_size = raw.get("page_size")
        if not isinstance(page_size, int) or not (1 <= page_size <= 1000):
            raise ManifestError(f"{prefix}.page_size must be 1..1000")

        max_pages = raw.get("max_pages")
        if not isinstance(max_pages, int) or not (1 <= max_pages <= 1000):
            raise ManifestError(f"{prefix}.max_pages must be 1..1000")

        window_start = _parse_utc(raw.get("window_start", ""), f"{prefix}.window_start")
        window_end = _parse_utc(raw.get("window_end", ""), f"{prefix}.window_end")
        if window_start >= window_end:
            raise ManifestError(f"{prefix}.window_start must be before window_end")

        max_window_minutes = raw.get("max_window_minutes")
        if not isinstance(max_window_minutes, int) or max_window_minutes < 1:
            raise ManifestError(f"{prefix}.max_window_minutes must be a positive integer")
        duration_minutes = (window_end - window_start).total_seconds() / 60
        if duration_minutes > max_window_minutes:
            raise ManifestError(
                f"{prefix} window duration {duration_minutes}m exceeds max_window_minutes {max_window_minutes}"
            )

        params = raw.get("params")
        if params is not None and not isinstance(params, dict):
            raise ManifestError(f"{prefix}.params must be an object")

        time_boxed = raw.get("time_boxed", True)
        if not isinstance(time_boxed, bool):
            raise ManifestError(f"{prefix}.time_boxed must be a boolean")

        windows = method_windows.setdefault(method, [])
        for existing_start, existing_end in windows:
            if window_start < existing_end and window_end > existing_start:
                raise ManifestError(f"overlapping {method} windows: {dataset}")
        windows.append((window_start, window_end))

        result.append(WdtDataset(
            dataset=dataset,
            method=method,
            target_table=target_table,
            record_id_path=record_id_path,
            page_size=page_size,
            max_pages=max_pages,
            window_start=window_start,
            window_end=window_end,
            max_window_minutes=max_window_minutes,
            params=dict(params) if params else {},
            time_boxed=time_boxed,
        ))

    return tuple(result)


def load_manifest(path: Path) -> SourceManifest:
    raw_bytes = path.read_bytes()
    sha = hashlib.sha256(raw_bytes).hexdigest()

    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest is not valid UTF-8 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")

    allowed_keys = {"version", "dingtalk", "wdt"}
    unknown = set(data.keys()) - allowed_keys
    if unknown:
        raise ManifestError(f"unknown top-level keys: {sorted(unknown)}")

    version = data.get("version")
    if version != 1:
        raise ManifestError(f"manifest version must be 1 (got {version!r})")

    dingtalk = data.get("dingtalk")
    if not isinstance(dingtalk, dict):
        raise ManifestError("dingtalk must be an object")
    raw_bases = dingtalk.get("bases")
    if not isinstance(raw_bases, list):
        raise ManifestError("dingtalk.bases must be a list")

    wdt = data.get("wdt")
    if not isinstance(wdt, dict):
        raise ManifestError("wdt must be an object")
    raw_wdt_datasets = wdt.get("datasets")
    if not isinstance(raw_wdt_datasets, list):
        raise ManifestError("wdt.datasets must be a list")

    sheets = _load_dingtalk_bases(raw_bases, _KNOWN_TABLES)
    wdt_datasets = _load_wdt_datasets(raw_wdt_datasets)
    org = _load_dingtalk_org(dingtalk.get("org"))

    if org is not None and org.dataset in {sheet.dataset for sheet in sheets}:
        raise ManifestError(f"duplicate dataset: {org.dataset}")

    return SourceManifest(
        dingtalk_sheets=sheets,
        wdt_datasets=wdt_datasets,
        sha256=sha,
        dingtalk_org=org,
    )
