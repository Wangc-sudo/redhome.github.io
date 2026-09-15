"""人工报表导入模板（YAML，版本受控）。

一份模板回答「一张人工报表怎么进库」：

* ``dataset`` / ``target_table`` —— 数据集身份（raw 侧按 dataset 归档，
  mart 侧按 dataset 投影）；
* ``period_type`` —— 报表时间颗粒度（month / quarter / year），决定
  ``fact_manual_report`` 的 ``period_start`` / ``period_end`` 推导；
* ``columns`` —— 源列表头 → 标准字段的映射，含类型、必填、枚举；
* ``total_check`` —— 合计校验表达式（明细合计 vs 报表总计行）。

模板是**代码资产**，改模板即改契约，走评审。错误一律抛
:class:`ManualTemplateError`，消息只含字段名与取值，不含文件内容。

与 ``manifest.py`` 一致：解析即校验，坏模板在读到第一行数据之前就被拒绝。
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

#: 模板文件版本：结构一旦变更就 +1，旧模板当场拒绝（避免静默误读）。
TEMPLATE_VERSION = 1

PERIOD_TYPES = ("month", "quarter", "year")

#: 列的角色：期间 / 维度 / 指标。投影时只有 metric 会熔进窄表。
COLUMN_ROLES = ("period", "dimension", "metric")

COLUMN_TYPES = ("period", "string", "decimal", "int", "date")

DEFAULT_TEMPLATE_DIR = Path("docker/integration/manual-import-templates")

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: 占位表头前缀：通用模板 ``generic.yaml`` 用它标记「业务还没给表头」。
#: 带占位符的模板直接拒绝，避免有人拿未填模板 ``--apply`` 出一份假数据。
PLACEHOLDER_SOURCE_PREFIX = "待填"

#: 合计校验表达式：只支持 ``sum(<指标>) == total[.<指标>]`` 这一种形状。
#: 刻意**不用** eval——表达式来自版本受控模板，但求值器必须是死的。
_TOTAL_EXPRESSION_RE = re.compile(
    r"^sum\((?P<metric>[a-z_]+)\)\s*==\s*total(?:\.(?P<field>[a-z_]+))?$"
)

_PERIOD_PATTERNS = {
    "month": re.compile(r"^(\d{4})-(\d{2})$"),
    "quarter": re.compile(r"^(\d{4})-[Qq]([1-4])$"),
    "year": re.compile(r"^(\d{4})$"),
}


class ManualTemplateError(ValueError):
    """模板文件非法（消息只含字段名与取值，不含文件内容）。"""


class PeriodError(ValueError):
    """期间文本与模板的 ``period_type`` 不符。"""


def parse_period(period_type: str, text: str) -> tuple[date, date]:
    """把 ``2026-08`` / ``2026-Q3`` / ``2026`` 解析成 ``(起, 止)``。

    起止日期是**闭区间**，供 ``fact_manual_report`` 的
    ``period_start`` / ``period_end`` 直接落库，BI 侧不必再推导。
    """
    if period_type not in _PERIOD_PATTERNS:
        raise PeriodError(f"unsupported period_type: {period_type}")
    raw = "" if text is None else str(text).strip()
    matched = _PERIOD_PATTERNS[period_type].match(raw)
    if not matched:
        raise PeriodError(
            f"{raw!r} is not a valid {period_type} period"
        )

    if period_type == "month":
        year, month = int(matched.group(1)), int(matched.group(2))
        if not 1 <= month <= 12:
            raise PeriodError(f"{raw!r} month must be 01..12")
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last_day)

    if period_type == "quarter":
        year, quarter = int(matched.group(1)), int(matched.group(2))
        last_month = quarter * 3
        last_day = calendar.monthrange(year, last_month)[1]
        return date(year, last_month - 2, 1), date(year, last_month, last_day)

    year = int(matched.group(1))
    return date(year, 1, 1), date(year, 12, 31)


def format_period(period_type: str, text: str) -> str:
    """归一化期间文本（``2026-q3`` → ``2026-Q3``），供落库与对账。"""
    if period_type == "quarter":
        start, _ = parse_period(period_type, text)
        return f"{start.year}-Q{(start.month - 1) // 3 + 1}"
    return "" if text is None else str(text).strip()


@dataclass(frozen=True)
class TotalCheck:
    """合计校验：明细行求和 vs 报表总计行。

    ``tolerance`` 是**相对**容差（默认 0.001，即 0.1%），并以 0.01 为绝对
    下限——避免小额报表被相对容差放过、大额报表被四舍五入卡死。
    """

    metric: str
    total_row: str
    tolerance: Decimal = Decimal("0.001")
    expression: str = ""

    @property
    def total_field(self) -> str:
        return self.metric


@dataclass(frozen=True)
class TemplateColumn:
    """一列的映射与约束。"""

    source: str
    field: str
    role: str
    type: str
    required: bool = False
    enum: tuple[str, ...] = ()
    unit: str = ""
    label: str = ""


@dataclass(frozen=True)
class ManualTemplate:
    """一份人工报表导入模板。"""

    dataset: str
    target_table: str
    period_type: str
    columns: tuple[TemplateColumn, ...]
    version: int = TEMPLATE_VERSION
    description: str = ""
    total_check: TotalCheck | None = None
    extreme_value_threshold: Decimal | None = None
    path: Path | None = None

    @property
    def period_columns(self) -> tuple[TemplateColumn, ...]:
        return tuple(c for c in self.columns if c.role == "period")

    @property
    def dimension_columns(self) -> tuple[TemplateColumn, ...]:
        return tuple(c for c in self.columns if c.role == "dimension")

    @property
    def metric_columns(self) -> tuple[TemplateColumn, ...]:
        return tuple(c for c in self.columns if c.role == "metric")

    def column_by_field(self, field: str) -> TemplateColumn:
        for column in self.columns:
            if column.field == field:
                return column
        raise KeyError(f"unregistered field: {field}")


def load_template(path) -> ManualTemplate:
    """解析并校验 *path* 处的模板文件。"""
    import yaml

    target = Path(path)
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManualTemplateError("template is not a readable file") from exc
    except yaml.YAMLError as exc:
        raise ManualTemplateError("template is not valid YAML") from exc

    if not isinstance(raw, dict):
        raise ManualTemplateError("template must be a mapping")

    unknown = {k for k in raw if not str(k).startswith("_")} - {
        "version", "dataset", "target_table", "period_type", "description",
        "columns", "total_check", "extreme_value_threshold",
    }
    if unknown:
        raise ManualTemplateError(f"unknown template keys: {sorted(unknown)}")

    version = raw.get("version")
    if version != TEMPLATE_VERSION:
        raise ManualTemplateError(
            f"template version must be {TEMPLATE_VERSION} (got {version!r})"
        )

    dataset = _require_identifier(raw.get("dataset"), "dataset")
    target_table = _require_identifier(raw.get("target_table"), "target_table")

    period_type = raw.get("period_type")
    if period_type not in PERIOD_TYPES:
        raise ManualTemplateError(
            f"period_type must be one of {list(PERIOD_TYPES)} (got {period_type!r})"
        )

    description = raw.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ManualTemplateError("description must be a string")

    columns = _load_columns(raw.get("columns"), dataset)
    total_check = _load_total_check(raw.get("total_check"), dataset, columns)
    threshold = _load_threshold(raw.get("extreme_value_threshold"))

    return ManualTemplate(
        dataset=dataset,
        target_table=target_table,
        period_type=period_type,
        columns=columns,
        version=TEMPLATE_VERSION,
        description=description,
        total_check=total_check,
        extreme_value_threshold=threshold,
        path=target,
    )


def load_template_by_name(name: str, directory=None) -> ManualTemplate:
    """按数据集名加载 ``<directory>/<name>.yaml``。

    *name* 必须是标识符——既是命名约定，也挡掉 ``../`` 之类的路径穿越。
    """
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ManualTemplateError(f"invalid template name: {name!r}")
    base = Path(directory) if directory else DEFAULT_TEMPLATE_DIR
    path = base / f"{name}.yaml"
    if not path.is_file():
        raise ManualTemplateError(f"unregistered manual import template: {name}")
    return load_template(path)


# ---------------------------------------------------------------------------
# 内部解析
# ---------------------------------------------------------------------------

def _require_identifier(value, label):
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise ManualTemplateError(f"{label} must match [a-z][a-z0-9_]*")
    return value


def _load_columns(raw, dataset):
    if not isinstance(raw, list) or not raw:
        raise ManualTemplateError("columns must be a non-empty list")

    columns: list[TemplateColumn] = []
    seen_fields: set[str] = set()
    seen_sources: set[str] = set()

    for index, item in enumerate(raw):
        prefix = f"columns[{index}]"
        if not isinstance(item, dict):
            raise ManualTemplateError(f"{prefix} must be a mapping")

        source = item.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ManualTemplateError(f"{prefix}.source is required")
        if source in seen_sources:
            raise ManualTemplateError(f"duplicate column source: {source}")
        if source.strip().startswith(PLACEHOLDER_SOURCE_PREFIX):
            raise ManualTemplateError(
                f"{prefix}.source is a placeholder: replace "
                f"{source.strip()!r} with the real report header"
            )
        seen_sources.add(source)

        field = _require_identifier(item.get("field"), f"{prefix}.field")
        if field in seen_fields:
            raise ManualTemplateError(f"duplicate column field: {field}")
        seen_fields.add(field)

        role = item.get("role")
        if role not in COLUMN_ROLES:
            raise ManualTemplateError(
                f"{prefix}.role must be one of {list(COLUMN_ROLES)}"
            )
        column_type = item.get("type")
        if column_type not in COLUMN_TYPES:
            raise ManualTemplateError(
                f"{prefix}.type must be one of {list(COLUMN_TYPES)}"
            )
        if role == "metric" and column_type not in ("decimal", "int"):
            raise ManualTemplateError(
                f"{prefix}: a metric column must be decimal or int"
            )

        required = item.get("required", False)
        if not isinstance(required, bool):
            raise ManualTemplateError(f"{prefix}.required must be a boolean")

        enum = _load_enum(item.get("enum"), prefix)
        unit = item.get("unit", "")
        if unit is None:
            unit = ""
        if not isinstance(unit, str):
            raise ManualTemplateError(f"{prefix}.unit must be a string")
        label = item.get("label", "")
        if label is None:
            label = ""
        if not isinstance(label, str):
            raise ManualTemplateError(f"{prefix}.label must be a string")

        columns.append(TemplateColumn(
            source=source.strip(),
            field=field,
            role=role,
            type=column_type,
            required=required,
            enum=enum,
            unit=unit,
            label=label,
        ))

    roles = [column.role for column in columns]
    if roles.count("period") > 1:
        raise ManualTemplateError("a template carries at most one period column")
    if "dimension" not in roles:
        raise ManualTemplateError(f"{dataset}: at least one dimension column")
    if "metric" not in roles:
        raise ManualTemplateError(f"{dataset}: at least one metric column")
    return tuple(columns)


def _load_enum(raw, prefix):
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise ManualTemplateError(f"{prefix}.enum must be a non-empty list")
    values = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise ManualTemplateError(f"{prefix}.enum entries must be non-empty strings")
        values.append(value.strip())
    return tuple(values)


def _load_total_check(raw, dataset, columns):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManualTemplateError("total_check must be a mapping")

    expression = raw.get("expression")
    if not isinstance(expression, str):
        raise ManualTemplateError("total_check.expression is required")
    matched = _TOTAL_EXPRESSION_RE.match(expression.strip())
    if not matched:
        raise ManualTemplateError(
            "total_check.expression must look like 'sum(<metric>) == total[.<field>]'"
        )

    metric = matched.group("metric")
    fields = {column.field for column in columns if column.role == "metric"}
    if metric not in fields:
        raise ManualTemplateError(
            f"{dataset}: total_check sums unknown metric {metric!r}"
        )

    total_row = raw.get("total_row")
    if not isinstance(total_row, str) or not total_row.strip():
        raise ManualTemplateError("total_check.total_row is required")

    tolerance = raw.get("tolerance", "0.001")
    try:
        parsed = Decimal(str(tolerance))
    except (InvalidOperation, ValueError):
        raise ManualTemplateError("total_check.tolerance must be numeric") from None
    if not parsed.is_finite() or parsed < 0:
        raise ManualTemplateError("total_check.tolerance must be a non-negative number")

    return TotalCheck(
        metric=metric,
        total_row=total_row.strip(),
        tolerance=parsed,
        expression=expression.strip(),
    )


def _load_threshold(raw):
    if raw is None:
        return None
    try:
        parsed = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ManualTemplateError(
            "extreme_value_threshold must be numeric"
        ) from None
    if not parsed.is_finite() or parsed <= 0:
        raise ManualTemplateError(
            "extreme_value_threshold must be a positive number"
        )
    return parsed
