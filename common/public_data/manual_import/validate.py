"""人工报表校验：原始行 → 结构化校验报告 + 可导入行。

校验器**不抛裸异常**：任何问题（类型不符、必填缺失、枚举越界、重复行、
合计对不上、负值/极端值）都变成 :class:`Issue` 进报告，CLI 原样打印，
业务自己照着行号改表。只有「文件缺列」这种无法逐行定位的问题会挂在
``row=None`` 上。

严重度分两级：

* ``error`` —— 阻断落库（该行不进库；有 error 则整批不导入）；
* ``warning`` —— 提示但不阻断（费用为负、数值异常大），进库且留痕。

合计校验（``total_check``）：明细行的指标求和，与报表里「合计」行的同一
指标比对，差异超容差即报错。总计行本身**不导入**（它是派生行，导入会
重复计数）。
"""

import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from common.public_data.manual_import.loader import (
    LoadedTable,
    missing_source_columns,
)
from common.public_data.manual_import.template import format_period, parse_period

ERROR = "error"
WARNING = "warning"

#: 合计校验的绝对下限（元）：小额报表不被相对容差放过。
_ABSOLUTE_TOLERANCE_FLOOR = Decimal("0.01")

_ISSUE_CODES = (
    "missing_column",
    "missing_required",
    "bad_type",
    "not_in_enum",
    "duplicate_row",
    "period_mismatch",
    "total_mismatch",
    "negative_value",
    "extreme_value",
)


@dataclass(frozen=True)
class Issue:
    """一条校验问题。``row`` 为 ``None`` 表示整表级问题（如缺列）。"""

    code: str
    message: str
    severity: str = ERROR
    row: int | None = None
    column: str = ""

    def format(self) -> str:
        location = "row=-" if self.row is None else f"row={self.row}"
        column = self.column or "-"
        return f"{self.severity} {location} column={column} code={self.code} {self.message}"


@dataclass(frozen=True)
class NormalizedRow:
    """一行通过校验、可直接落库的数据（标准字段名 → 值）。"""

    row_no: int
    fields: dict


@dataclass(frozen=True)
class ValidationReport:
    """一次校验的结构化结果。"""

    dataset: str
    template_version: int
    period: str
    period_start: date
    period_end: date
    total_rows: int
    rows: tuple[NormalizedRow, ...] = ()
    issues: tuple[Issue, ...] = ()

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == ERROR)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == WARNING)

    @property
    def rows_bad(self) -> int:
        """含 error 的原始数据行数（同一行多个 error 只算一次）。"""
        return len({issue.row for issue in self.errors if issue.row is not None})

    @property
    def rows_ok(self) -> int:
        return len(self.rows)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary_lines(self, limit: int = 20) -> list[str]:
        """供 CLI 打印的报告行（先摘要后明细，超 *limit* 条折叠）。"""
        status = "ok" if self.ok else "rejected"
        lines = [
            f"dataset={self.dataset} template_version={self.template_version}"
            f" period={self.period} period_start={self.period_start}"
            f" period_end={self.period_end} rows_read={self.total_rows}"
            f" rows_ok={self.rows_ok} rows_bad={self.rows_bad}"
            f" errors={len(self.errors)} warnings={len(self.warnings)}"
            f" status={status}"
        ]
        shown = self.issues[:limit]
        lines.extend(issue.format() for issue in shown)
        hidden = len(self.issues) - len(shown)
        if hidden > 0:
            lines.append(f"... and {hidden} more issue(s)")
        return lines


def validate_table(
    table: LoadedTable,
    template,
    period: str,
) -> ValidationReport:
    """按 *template* 校验 *table*，期间一律以 CLI 传入的 *period* 为准。"""
    issues: list[Issue] = []
    normalized_period = format_period(template.period_type, period)
    period_start, period_end = parse_period(template.period_type, period)

    missing = missing_source_columns(table, template)
    for source in missing:
        issues.append(Issue(
            code="missing_column",
            message=f"文件缺少模板声明的源列: {source}",
        ))

    rows: list[NormalizedRow] = []
    if not missing:
        rows = _normalize_rows(table, template, normalized_period, issues)

    _check_duplicates(rows, template, issues)
    if template.total_check is not None and not missing:
        rows = _check_total(rows, template, issues)

    return ValidationReport(
        dataset=template.dataset,
        template_version=template.version,
        period=normalized_period,
        period_start=period_start,
        period_end=period_end,
        total_rows=len(table.rows),
        rows=tuple(rows),
        issues=tuple(issues),
    )


# ---------------------------------------------------------------------------
# 内部校验
# ---------------------------------------------------------------------------

def _normalize_rows(table, template, period, issues) -> list[NormalizedRow]:
    rows: list[NormalizedRow] = []
    for source_row in table.rows:
        row_issues: list[Issue] = []
        fields: dict = {}
        # 报表总计行的维度取值是「合计」这类标记，不是枚举里的真实维度；
        # 它只用于对账（随后被 _check_total 剔除），因此豁免枚举校验。
        is_total_row = _is_total_row(source_row, template)
        for column in template.columns:
            raw = source_row.values.get(column.source)
            text = _as_text(raw)
            if not text:
                if column.required:
                    row_issues.append(Issue(
                        code="missing_required",
                        message="必填字段为空",
                        severity=ERROR,
                        row=source_row.row_no,
                        column=column.field,
                    ))
                continue  # 选填留空：不进库，也不算错

            value = _convert(column, text, source_row.row_no, row_issues)
            if value is None:
                continue
            if column.role == "period":
                if str(value) != period:
                    row_issues.append(Issue(
                        code="period_mismatch",
                        message=f"行内期间 {value} 与命令行期间 {period} 不一致",
                        severity=ERROR,
                        row=source_row.row_no,
                        column=column.field,
                    ))
                    continue
            if column.enum and not is_total_row and str(value) not in column.enum:
                row_issues.append(Issue(
                    code="not_in_enum",
                    message="取值不在模板枚举内",
                    severity=ERROR,
                    row=source_row.row_no,
                    column=column.field,
                ))
                continue
            if column.role == "metric":
                _check_metric_magnitude(
                    value, column, template, source_row.row_no, row_issues
                )
            fields[column.field] = value

        issues.extend(row_issues)
        if any(issue.severity == ERROR for issue in row_issues):
            continue  # 有错的行整行不进库
        if fields:
            rows.append(NormalizedRow(row_no=source_row.row_no, fields=fields))
    return rows


def _is_total_row(source_row, template) -> bool:
    """按**原始维度取值**判断某行是否报表总计行（早于类型转换）。"""
    check = template.total_check
    if check is None:
        return False
    values = [
        _as_text(source_row.values.get(column.source))
        for column in template.columns
        if column.role == "dimension"
    ]
    return "|".join(values) == check.total_row


def _check_duplicates(rows, template, issues) -> None:
    """同一期间 + 同一维度组合出现两次 → 报错（重复行会双重计数）。"""
    keys = tuple(
        column.field for column in template.columns if column.role == "dimension"
    )
    seen: dict[tuple, int] = {}
    for row in rows:
        key = tuple(str(row.fields.get(name, "")) for name in keys)
        if not any(key):
            continue
        if key in seen:
            issues.append(Issue(
                code="duplicate_row",
                message=f"与 row={seen[key]} 的维度组合重复",
                severity=ERROR,
                row=row.row_no,
                column="+".join(keys),
            ))
            continue
        seen[key] = row.row_no


def _check_total(rows, template, issues):
    """明细合计 vs 报表总计行：差异超容差即报错，并剔除总计行。"""
    check = template.total_check
    dimension_fields = [
        column.field for column in template.columns if column.role == "dimension"
    ]

    def dimension_of(row) -> str:
        return "|".join(str(row.fields.get(name, "")) for name in dimension_fields)

    total_rows = [row for row in rows if dimension_of(row) == check.total_row]
    detail_rows = [row for row in rows if dimension_of(row) != check.total_row]

    if not total_rows:
        issues.append(Issue(
            code="total_mismatch",
            message=f"未找到总计行（维度取值应为 {check.total_row}）",
            severity=ERROR,
        ))
        return detail_rows

    expected = _metric_value(total_rows[0], check.metric)
    if expected is None:
        issues.append(Issue(
            code="total_mismatch",
            message="总计行的被校验指标为空",
            severity=ERROR,
            row=total_rows[0].row_no,
            column=check.metric,
        ))
        return detail_rows

    actual = Decimal("0")
    for row in detail_rows:
        value = _metric_value(row, check.metric)
        if value is not None:
            actual += value

    allowed = max(_ABSOLUTE_TOLERANCE_FLOOR, abs(expected) * check.tolerance)
    difference = abs(actual - expected)
    if difference > allowed:
        issues.append(Issue(
            code="total_mismatch",
            message=(
                f"明细合计 {actual} 与总计行 {expected} 差异 {difference}"
                f" 超过容差 {allowed}"
            ),
            severity=ERROR,
            row=total_rows[0].row_no,
            column=check.metric,
        ))
    return detail_rows


def _metric_value(row, metric):
    value = row.fields.get(metric)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _check_metric_magnitude(value, column, template, row_no, issues) -> None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):  # pragma: no cover - 类型已校验
        return
    if number < 0:
        issues.append(Issue(
            code="negative_value",
            message=f"指标为负（{number}），请确认是否红冲/退货",
            severity=WARNING,
            row=row_no,
            column=column.field,
        ))
    threshold = template.extreme_value_threshold
    if threshold is not None and abs(number) > threshold:
        issues.append(Issue(
            code="extreme_value",
            message=f"指标绝对值 {abs(number)} 超过阈值 {threshold}，请确认单位",
            severity=WARNING,
            row=row_no,
            column=column.field,
        ))


def _as_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _convert(column, text, row_no, issues):
    """把单元格文本转成标准字段值；失败时挂一条 error 并返回 ``None``。"""
    if column.type == "period":
        return text
    if column.type == "string":
        return text
    if column.type == "date":
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            issues.append(Issue(
                code="bad_type",
                message="不是合法日期（应形如 2026-08-31）",
                severity=ERROR,
                row=row_no,
                column=column.field,
            ))
            return None
    if column.type in ("decimal", "int"):
        number = _to_decimal(text)
        if number is None:
            issues.append(Issue(
                code="bad_type",
                message="不是合法数值",
                severity=ERROR,
                row=row_no,
                column=column.field,
            ))
            return None
        if column.type == "int":
            if number != number.to_integral_value():
                issues.append(Issue(
                    code="bad_type",
                    message="不是整数",
                    severity=ERROR,
                    row=row_no,
                    column=column.field,
                ))
                return None
            return int(number)
        return number
    return text  # pragma: no cover - COLUMN_TYPES 已在模板层收口


def _to_decimal(text: str):
    """把报表里的金额文本转成 :class:`Decimal`。

    容忍人工报表的常见写法：全角字符、千分位、货币符号、括号表示负数。
    不做任何单位换算（万元/元由模板与业务约定，代码绝不猜）。
    """
    cleaned = unicodedata.normalize("NFKC", text).strip()
    cleaned = cleaned.replace(",", "").replace("，", "").replace(" ", "")
    for symbol in ("¥", "￥", "元", "$"):
        cleaned = cleaned.replace(symbol, "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    if not cleaned:
        return None
    try:
        number = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return -number if negative else number
