"""人工报表导入通道（C 类数据源）的单测。

五组（对应实现分层）：

* ``TemplateTests`` —— 模板解析与契约校验（含仓库内两个真实模板）；
* ``LoaderTests`` —— CSV/XLSX 读取、行号、摘要、缺列探测；
* ``ValidateTests`` —— 类型/必填/枚举/重复/合计/负值/极端值 → 结构化报告；
* ``RepositoryTests`` / ``ProjectorTests`` —— 假连接上的 SQL 形状与窄表 melt；
* ``ServiceTests`` / ``ImportManualCliTests`` / ``ManualMigrationTests``
  —— 幂等、覆盖语义、CLI dry-run 与迁移注册。

全部用内存 fake，**不连真实 MySQL**。
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from common.public_data.cli import main
from common.public_data.live_migrations import (
    _MIGRATIONS,
    apply_live_migrations,
    apply_manual_migrations,
)
from common.public_data.manual_import.loader import (
    LoadedTable,
    ManualImportError,
    SourceRow,
    file_sha256,
    load_table,
    missing_source_columns,
)
from common.public_data.manual_import.projector import (
    FactRow,
    ManualProjector,
    build_fact_rows,
)
from common.public_data.manual_import.repository import (
    ManualImportRepository,
    ManualRow,
)
from common.public_data.manual_import.service import (
    STATUS_DUPLICATE,
    STATUS_DRY_RUN,
    STATUS_IMPORTED,
    STATUS_REJECTED,
    ManualImportResult,
    ManualImportService,
)
from common.public_data.manual_import.template import (
    PERIOD_TYPES,
    ManualTemplateError,
    PeriodError,
    format_period,
    load_template,
    load_template_by_name,
    parse_period,
)
from common.public_data.manual_import.validate import (
    ERROR,
    WARNING,
    ValidationReport,
    validate_table,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "docker" / "integration" / "manual-import-templates"

try:  # XLSX 用例依赖 openpyxl，缺失时跳过（CSV 通道不受影响）
    import openpyxl  # noqa: F401
    _HAS_OPENPYXL = True
except ImportError:  # pragma: no cover - 取决于部署环境
    _HAS_OPENPYXL = False


# ---------------------------------------------------------------------------
# 假的数据库与内存仓储
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, events, rows=None):
        self.executed = []
        self.events = events
        self._rows = list(rows or [])
        self.closed = False

    def execute(self, query, parameters=None):
        self.executed.append((query, parameters))
        self.events.append(("execute", query, parameters))

    def executemany(self, query, parameters=None):
        self.executed.append((query, parameters))
        self.events.append(("executemany", query, parameters))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, rows=None):
        self.events = []
        self.cursor_instance = FakeCursor(self.events, rows)
        self.commit_calls = 0
        self.rollback_calls = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commit_calls += 1
        self.events.append("commit")

    def rollback(self):
        self.rollback_calls += 1
        self.events.append("rollback")

    @property
    def executed(self):
        return self.cursor_instance.executed

    def sql_text(self) -> str:
        return "\n".join(query for query, _ in self.executed)


class InMemoryRepository:
    """内存 stub：与 :class:`ManualImportRepository` 同接口。"""

    def __init__(self):
        self.runs = {}
        self.rows = []
        self.deletes = []

    def find_run(self, dataset, period, file_sha256):
        for run in self.runs.values():
            if (
                run["dataset"] == dataset
                and run["period"] == period
                and run["file_sha256"] == file_sha256
            ):
                return Mock(
                    run_id=run["run_id"],
                    dataset=dataset,
                    period=period,
                    file_sha256=file_sha256,
                    status=run["status"],
                )
        return None

    def start_run(self, **kwargs):
        self.runs[kwargs["run_id"]] = dict(kwargs)

    def finish_run(self, run_id, *, rows_ok, rows_bad, status):
        self.runs[run_id].update(
            rows_ok=rows_ok, rows_bad=rows_bad, status=status
        )

    def replace_rows(self, *, run_id, dataset, period, rows, imported_at):
        self.deletes.append((dataset, period))
        self.rows = [
            (run_id, dataset, period, row) for row in rows
        ]
        return len(rows)


class InMemoryProjector:
    def __init__(self):
        self.calls = []

    def replace_facts(self, facts, *, synced_at):
        self.calls.append(list(facts))
        return len(facts)


# ---------------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------------

ECOMMERCE_CSV = (
    "月份,渠道,销售收入,毛利,费用\n"
    "2026-08,天猫,1000,300,50\n"
    "2026-08,京东,2000,600,80\n"
    "2026-08,合计,3000,900,130\n"
)


class ManualImportTestCase(unittest.TestCase):
    """提供临时目录、CSV 写入与模板加载的小工具。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write_csv(self, text, name="report.csv", encoding="utf-8"):
        path = self.tmp / name
        path.write_text(text, encoding=encoding)
        return path

    def template(self, name="ecommerce_monthly"):
        return load_template_by_name(name, TEMPLATE_DIR)


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------

class TemplateTests(ManualImportTestCase):
    def test_repository_templates_carry_the_monthly_contract(self):
        for name, dimension in (
            ("ecommerce_monthly", "channel"),
            ("restaurant_monthly", "store"),
        ):
            with self.subTest(template=name):
                template = load_template_by_name(name, TEMPLATE_DIR)

                self.assertEqual(name, template.dataset)
                self.assertEqual("month", template.period_type)
                self.assertEqual(1, template.version)
                fields = {column.field for column in template.columns}
                self.assertEqual(
                    {"period", dimension, "revenue", "gross_profit", "expense"},
                    fields,
                )
                self.assertEqual(
                    ("revenue", "gross_profit", "expense"),
                    tuple(c.field for c in template.metric_columns),
                )
                self.assertIsNotNone(template.total_check)
                self.assertEqual("revenue", template.total_check.metric)

    def test_period_and_metric_columns_are_split_by_role(self):
        template = self.template()

        self.assertEqual(("period",), tuple(c.field for c in template.period_columns))
        self.assertEqual(("channel",), tuple(c.field for c in template.dimension_columns))
        self.assertTrue(all(c.role == "metric" for c in template.metric_columns))

    def test_unknown_template_name_is_rejected(self):
        with self.assertRaises(ManualTemplateError):
            load_template_by_name("no_such_dataset", TEMPLATE_DIR)

    def test_template_name_must_be_an_identifier(self):
        # 既是命名约定，也挡掉 ../ 之类的路径穿越
        for name in ("../secret", "Bad-Name", ""):
            with self.subTest(name=name):
                with self.assertRaises(ManualTemplateError):
                    load_template_by_name(name, TEMPLATE_DIR)

    def test_custom_template_directory_is_honoured(self):
        path = self.tmp / "custom.yaml"
        path.write_text(
            "version: 1\n"
            "dataset: custom_quarter\n"
            "target_table: manual_custom_quarter\n"
            "period_type: quarter\n"
            "columns:\n"
            "  - {source: 季度, field: period, role: period, type: period, required: true}\n"
            "  - {source: 板块, field: segment, role: dimension, type: string, required: true}\n"
            "  - {source: 预算, field: budget, role: metric, type: decimal, required: true}\n",
            encoding="utf-8",
        )

        template = load_template_by_name("custom", self.tmp)

        self.assertEqual("custom_quarter", template.dataset)
        self.assertEqual("quarter", template.period_type)

    def test_rejects_wrong_version(self):
        path = self.tmp / "bad.yaml"
        path.write_text(
            "version: 2\ndataset: x\ntarget_table: y\nperiod_type: month\n"
            "columns: []\n",
            encoding="utf-8",
        )

        with self.assertRaises(ManualTemplateError):
            load_template(path)

    def test_rejects_template_without_a_metric_column(self):
        path = self.tmp / "bad.yaml"
        path.write_text(
            "version: 1\ndataset: x\ntarget_table: y\nperiod_type: month\n"
            "columns:\n"
            "  - {source: 月, field: period, role: period, type: period}\n"
            "  - {source: 店, field: store, role: dimension, type: string}\n",
            encoding="utf-8",
        )

        with self.assertRaises(ManualTemplateError):
            load_template(path)

    def test_rejects_unparseable_total_expression(self):
        path = self.tmp / "bad.yaml"
        path.write_text(
            "version: 1\ndataset: x\ntarget_table: y\nperiod_type: month\n"
            "total_check:\n"
            "  expression: \"__import__('os').system('rm -rf /')\"\n"
            "  total_row: 合计\n"
            "columns:\n"
            "  - {source: 店, field: store, role: dimension, type: string}\n"
            "  - {source: 额, field: revenue, role: metric, type: decimal}\n",
            encoding="utf-8",
        )

        # 表达式求值器是死的：只认 sum(x) == total[.y]，绝不 eval
        with self.assertRaises(ManualTemplateError):
            load_template(path)

    def test_total_check_must_reference_a_declared_metric(self):
        path = self.tmp / "bad.yaml"
        path.write_text(
            "version: 1\ndataset: x\ntarget_table: y\nperiod_type: month\n"
            "total_check:\n"
            "  expression: \"sum(nope) == total.nope\"\n"
            "  total_row: 合计\n"
            "columns:\n"
            "  - {source: 店, field: store, role: dimension, type: string}\n"
            "  - {source: 额, field: revenue, role: metric, type: decimal}\n",
            encoding="utf-8",
        )

        with self.assertRaises(ManualTemplateError):
            load_template(path)

    def test_parse_period_bounds(self):
        self.assertEqual(
            (date(2026, 8, 1), date(2026, 8, 31)), parse_period("month", "2026-08")
        )
        self.assertEqual(
            (date(2026, 7, 1), date(2026, 9, 30)), parse_period("quarter", "2026-Q3")
        )
        self.assertEqual(
            (date(2026, 1, 1), date(2026, 3, 31)), parse_period("quarter", "2026-q1")
        )
        self.assertEqual(
            (date(2025, 1, 1), date(2025, 12, 31)), parse_period("year", "2025")
        )

    def test_parse_period_rejects_mismatched_text(self):
        for period_type, text in (
            ("month", "2026-13"),
            ("month", "2026-Q1"),
            ("quarter", "2026-08"),
            ("year", "2026-08"),
        ):
            with self.subTest(period=text):
                with self.assertRaises(PeriodError):
                    parse_period(period_type, text)

    def test_quarter_text_is_normalised(self):
        self.assertEqual("2026-Q3", format_period("quarter", "2026-q3"))
        self.assertEqual("2026-08", format_period("month", " 2026-08 "))

    def test_underscore_doc_keys_are_ignored(self):
        path = self.tmp / "doc.yaml"
        path.write_text(
            "version: 1\n_说明: 文档键\ndataset: x\ntarget_table: y\n"
            "period_type: month\n"
            "columns:\n"
            "  - {source: 店, field: store, role: dimension, type: string, _说明: 门店}\n"
            "  - {source: 额, field: revenue, role: metric, type: decimal}\n",
            encoding="utf-8",
        )

        template = load_template(path)

        self.assertEqual(2, len(template.columns))


class GenericTemplateTests(ManualImportTestCase):
    """通用占位模板：让 ⑥⑨⑪⑬ 自助接入，且未填时不能误用。"""

    def filled_copy(self, period_type="month"):
        text = (TEMPLATE_DIR / "generic.yaml").read_text(encoding="utf-8")
        for placeholder, header in (
            ("待填-期间", "月份"),
            ("待填-维度", "门店"),
            ("待填-指标1", "收入"),
            ("待填-指标2", "费用"),
        ):
            text = text.replace(placeholder, header)
        path = self.tmp / "filled.yaml"
        path.write_text(
            text.replace("period_type: month", f"period_type: {period_type}"),
            encoding="utf-8",
        )
        return load_template(path)

    def test_placeholder_template_is_rejected_until_filled(self):
        with self.assertRaises(ManualTemplateError):
            load_template_by_name("generic", TEMPLATE_DIR)

    def test_filled_generic_template_loads_with_the_monthly_shape(self):
        template = self.filled_copy()

        self.assertEqual("generic", template.dataset)
        self.assertEqual(("period", "dimension", "metric", "metric"),
                         tuple(c.role for c in template.columns))
        self.assertEqual("元", template.metric_columns[0].unit)

    def test_generic_template_covers_all_three_period_types(self):
        for period_type in PERIOD_TYPES:
            with self.subTest(period_type=period_type):
                self.assertEqual(period_type, self.filled_copy(period_type).period_type)

    def test_generic_does_not_shadow_the_delivered_templates(self):
        ecommerce = load_template_by_name("ecommerce_monthly", TEMPLATE_DIR)
        restaurant = load_template_by_name("restaurant_monthly", TEMPLATE_DIR)

        self.assertEqual("ecommerce_monthly", ecommerce.dataset)
        self.assertEqual("restaurant_monthly", restaurant.dataset)
        self.assertEqual("month", ecommerce.period_type)
        self.assertEqual(
            ("revenue", "gross_profit", "expense"),
            tuple(c.field for c in ecommerce.metric_columns),
        )


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------

class LoaderTests(ManualImportTestCase):
    def test_csv_rows_are_numbered_from_the_first_data_row(self):
        path = self.write_csv(ECOMMERCE_CSV)

        table = load_table(path)

        self.assertEqual(("月份", "渠道", "销售收入", "毛利", "费用"), table.header)
        self.assertEqual(3, len(table.rows))
        self.assertEqual([2, 3, 4], [row.row_no for row in table.rows])
        self.assertEqual("天猫", table.rows[0].values["渠道"])

    def test_blank_lines_are_skipped(self):
        path = self.write_csv(ECOMMERCE_CSV + "\n")

        self.assertEqual(3, len(load_table(path).rows))

    def test_bom_and_padded_headers_are_tolerated(self):
        path = self.write_csv(ECOMMERCE_CSV, encoding="utf-8-sig")

        table = load_table(path)

        self.assertEqual("月份", table.header[0])

    def test_sha256_identifies_the_file_bytes(self):
        path = self.write_csv(ECOMMERCE_CSV)
        other = self.write_csv(ECOMMERCE_CSV + "2026-08,私域,10,1,0\n", name="other.csv")

        self.assertEqual(file_sha256(path), load_table(path).sha256)
        self.assertNotEqual(file_sha256(path), file_sha256(other))

    def test_missing_file_is_rejected(self):
        with self.assertRaises(ManualImportError):
            load_table(self.tmp / "nope.csv")

    def test_unsupported_suffix_is_rejected(self):
        path = self.tmp / "report.pdf"
        path.write_text("x", encoding="utf-8")

        with self.assertRaises(ManualImportError):
            load_table(path)

    @unittest.skipUnless(_HAS_OPENPYXL, "openpyxl is not installed")
    def test_xlsx_first_sheet_is_read(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["月份", "渠道", "销售收入", "毛利", "费用"])
        sheet.append(["2026-08", "天猫", 1000, 300, 50])
        sheet.append(["2026-08", "合计", 1000, 300, 50])
        path = self.tmp / "report.xlsx"
        workbook.save(path)

        table = load_table(path)

        self.assertEqual(2, len(table.rows))
        self.assertEqual(1000, table.rows[0].values["销售收入"])

    def test_missing_source_columns_are_detected(self):
        path = self.write_csv("月份,渠道\n2026-08,天猫\n")
        table = load_table(path)

        missing = missing_source_columns(table, self.template())

        self.assertEqual(("销售收入", "毛利", "费用"), missing)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

class ValidateTests(ManualImportTestCase):
    def report(self, csv_text=ECOMMERCE_CSV, period="2026-08", name="ecommerce_monthly"):
        table = load_table(self.write_csv(csv_text))
        return validate_table(table, self.template(name), period)

    def test_clean_report_normalises_rows_and_drops_the_total_row(self):
        report = self.report()

        self.assertTrue(report.ok)
        self.assertEqual([], list(report.warnings))
        self.assertEqual(2, report.rows_ok)      # 合计行不导入
        self.assertEqual(3, report.total_rows)
        self.assertEqual(date(2026, 8, 1), report.period_start)
        self.assertEqual(date(2026, 8, 31), report.period_end)
        self.assertEqual(Decimal("1000"), report.rows[0].fields["revenue"])
        self.assertEqual("天猫", report.rows[0].fields["channel"])

    def test_missing_required_value_blocks_the_row(self):
        report = self.report(
            "月份,渠道,销售收入,毛利,费用\n"
            "2026-08,天猫,1000,300,50\n"
            "2026-08,,2000,600,80\n"
            "2026-08,合计,1000,300,50\n"  # 坏行不计入明细，合计与剩余明细对齐
        )

        self.assertFalse(report.ok)
        missing = [i for i in report.errors if i.code == "missing_required"]
        self.assertEqual(1, len(missing))
        self.assertEqual(3, missing[0].row)
        self.assertEqual("channel", missing[0].column)
        self.assertEqual(1, report.rows_bad)

    def test_non_numeric_metric_is_reported_with_its_row(self):
        report = self.report(
            "月份,渠道,销售收入,毛利,费用\n"
            "2026-08,天猫,待补,300,50\n"
            "2026-08,合计,3000,900,130\n"
        )

        bad = [i for i in report.errors if i.code == "bad_type"]
        self.assertEqual(1, len(bad))
        self.assertEqual(2, bad[0].row)
        self.assertEqual("revenue", bad[0].column)

    def test_thousand_separators_and_currency_symbols_are_accepted(self):
        report = self.report(
            "月份,渠道,销售收入,毛利,费用\n"
            "2026-08,天猫,\"1,000\",300,50\n"
            "2026-08,京东,2000,600,80\n"
            "2026-08,合计,3000,900,130\n"
        )

        self.assertTrue(report.ok, report.issues)
        self.assertEqual(Decimal("1000"), report.rows[0].fields["revenue"])

    def test_value_outside_the_enum_is_rejected(self):
        report = self.report(
            "月份,渠道,销售收入,毛利,费用\n"
            "2026-08,未知渠道,1000,300,50\n"
            "2026-08,合计,1000,300,50\n"
        )

        self.assertFalse(report.ok)
        self.assertEqual("not_in_enum", report.errors[0].code)
        self.assertEqual(2, report.errors[0].row)

    def test_duplicate_dimension_rows_are_rejected(self):
        report = self.report(
            "月份,渠道,销售收入,毛利,费用\n"
            "2026-08,天猫,1000,300,50\n"
            "2026-08,天猫,1000,300,50\n"
            "2026-08,合计,2000,600,100\n"
        )

        duplicated = [i for i in report.errors if i.code == "duplicate_row"]
        self.assertEqual(1, len(duplicated))
        self.assertEqual(3, duplicated[0].row)
        self.assertEqual(2, report.rows_ok)  # 首行 + 合计行（合计行随后被剔除）

    def test_total_mismatch_is_rejected(self):
        report = self.report(
            "月份,渠道,销售收入,毛利,费用\n"
            "2026-08,天猫,1000,300,50\n"
            "2026-08,京东,2000,600,80\n"
            "2026-08,合计,3500,900,130\n"  # 明细 3000 vs 总计 3500
        )

        self.assertFalse(report.ok)
        mismatch = [i for i in report.errors if i.code == "total_mismatch"]
        self.assertEqual(1, len(mismatch))
        self.assertIn("差异", mismatch[0].message)

    def test_rounding_within_tolerance_is_accepted(self):
        report = self.report(
            "月份,渠道,销售收入,毛利,费用\n"
            "2026-08,天猫,1000.001,300,50\n"
            "2026-08,京东,2000,600,80\n"
            "2026-08,合计,3000,900,130\n"
        )

        self.assertTrue(report.ok, report.issues)

    def test_missing_total_row_is_rejected(self):
        report = self.report(
            "月份,渠道,销售收入,毛利,费用\n"
            "2026-08,天猫,1000,300,50\n"
        )

        self.assertFalse(report.ok)
        self.assertEqual("total_mismatch", report.errors[0].code)
        self.assertIsNone(report.errors[0].row)

    def test_negative_and_extreme_metrics_warn_without_blocking(self):
        # 合计行与明细对齐（对账口径），否则合计校验会先报 error
        report = self.report(
            "月份,渠道,销售收入,毛利,费用\n"
            "2026-08,天猫,-1000,300,50\n"
            "2026-08,京东,999999999999,600,80\n"
            "2026-08,合计,999999998999,900,130\n"
        )

        self.assertTrue(report.ok)
        self.assertEqual(
            {"negative_value", "extreme_value"},
            {issue.code for issue in report.warnings},
        )
        self.assertTrue(all(issue.severity == WARNING for issue in report.warnings))
        self.assertEqual(2, report.rows_ok)

    def test_row_period_must_match_the_cli_period(self):
        report = self.report(period="2026-09")

        self.assertFalse(report.ok)
        mismatch = [i for i in report.errors if i.code == "period_mismatch"]
        self.assertEqual(3, len(mismatch))

    def test_missing_source_column_blocks_whole_file(self):
        path = self.write_csv("月份,渠道\n2026-08,天猫\n")
        table = load_table(path)

        report = validate_table(table, self.template(), "2026-08")

        self.assertFalse(report.ok)
        self.assertEqual("missing_column", report.errors[0].code)
        self.assertEqual(0, report.rows_ok)

    def test_optional_metric_left_blank_is_not_written(self):
        report = self.report(
            "月份,渠道,销售收入,毛利,费用\n"
            "2026-08,天猫,1000,300,\n"
            "2026-08,合计,1000,300,0\n"
        )

        self.assertTrue(report.ok, report.issues)
        self.assertNotIn("expense", report.rows[0].fields)

    def test_summary_lines_carry_counts_and_status(self):
        report = self.report()

        lines = report.summary_lines()

        self.assertIn("dataset=ecommerce_monthly", lines[0])
        self.assertIn("rows_ok=2", lines[0])
        self.assertIn("status=ok", lines[0])

    def test_summary_lines_fold_long_issue_lists(self):
        csv = "月份,渠道,销售收入,毛利,费用\n" + "".join(
            f"2026-08,天猫{i},1,1,1\n" for i in range(30)
        ) + "2026-08,合计,30,30,30\n"

        lines = self.report(csv).summary_lines(limit=5)

        self.assertTrue(any(line.startswith("... and") for line in lines))


# ---------------------------------------------------------------------------
# repository
# ---------------------------------------------------------------------------

class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = FakeConnection()
        self.repository = ManualImportRepository(self.connection)

    def test_start_run_binds_every_value_as_a_parameter(self):
        self.repository.start_run(
            run_id="run-1",
            dataset="ecommerce_monthly",
            template_version=1,
            file_name="report.csv",
            file_sha256="a" * 64,
            period="2026-08",
            status="imported",
            imported_by="alice",
            imported_at="2026-09-15 10:00:00",
        )

        query, params = self.connection.executed[0]
        self.assertIn("INSERT INTO `manual_import_runs`", query)
        self.assertEqual(11, query.count("%s"))
        for secret in ("alice", "report.csv", "a" * 64, "2026-08"):
            self.assertNotIn(secret, query)
        self.assertEqual("run-1", params[0])
        self.assertEqual("alice", params[9])

    def test_find_run_returns_none_when_the_file_is_new(self):
        self.assertIsNone(self.repository.find_run("ecommerce_monthly", "2026-08", "a" * 64))

    def test_find_run_returns_the_previous_record(self):
        connection = FakeConnection(rows=[{
            "run_id": "run-0",
            "dataset": "ecommerce_monthly",
            "period": "2026-08",
            "file_sha256": "a" * 64,
            "status": "imported",
        }])

        found = ManualImportRepository(connection).find_run(
            "ecommerce_monthly", "2026-08", "a" * 64
        )

        self.assertEqual("run-0", found.run_id)
        self.assertEqual("imported", found.status)
        query, params = connection.executed[0]
        self.assertIn("WHERE `dataset` = %s AND `period` = %s", query)
        self.assertEqual(("ecommerce_monthly", "2026-08", "a" * 64), tuple(params))

    def test_replace_rows_deletes_the_period_before_inserting(self):
        rows = (
            ManualRow(row_no=2, fields={"channel": "天猫", "revenue": Decimal("1")},
                      source={"渠道": "天猫"}),
        )

        written = self.repository.replace_rows(
            run_id="run-1",
            dataset="ecommerce_monthly",
            period="2026-08",
            rows=rows,
            imported_at="2026-09-15 10:00:00",
        )

        self.assertEqual(1, written)
        events = self.connection.events
        self.assertIn("DELETE FROM `manual_import_row`", events[0][1])
        self.assertEqual(("ecommerce_monthly", "2026-08"), tuple(events[0][2]))
        self.assertIn("INSERT INTO `manual_import_row`", events[1][1])
        params = events[1][2]
        self.assertEqual("run-1", params[0])
        self.assertEqual(2, params[1])
        self.assertIn("天猫", params[5])

    def test_finish_run_updates_counters_and_status(self):
        self.repository.finish_run("run-1", rows_ok=10, rows_bad=2, status="imported")

        query, params = self.connection.executed[0]
        self.assertIn("UPDATE `manual_import_runs`", query)
        self.assertEqual((10, 2, "imported", "run-1"), tuple(params))


# ---------------------------------------------------------------------------
# projector
# ---------------------------------------------------------------------------

class ProjectorTests(ManualImportTestCase):
    def rows(self, report=None):
        return report.rows if report is not None else self._rows()

    def _rows(self):
        from common.public_data.manual_import.validate import NormalizedRow
        return (
            NormalizedRow(row_no=2, fields={
                "channel": "天猫", "revenue": Decimal("1000"),
                "gross_profit": Decimal("300"),
            }),
            NormalizedRow(row_no=3, fields={
                "channel": "京东", "revenue": Decimal("2000"),
                "gross_profit": Decimal("600"), "expense": Decimal("80"),
            }),
        )

    def test_metrics_are_melted_into_the_narrow_fact(self):
        facts = build_fact_rows(
            self._rows(),
            self.template(),
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            run_id="run-1",
        )

        self.assertEqual(5, len(facts))  # 2 指标 + 3 指标（缺费用不出行）
        first = facts[0]
        self.assertEqual("ecommerce_monthly", first.dataset)
        self.assertEqual("month", first.period_type)
        self.assertEqual(date(2026, 8, 1), first.period_start)
        self.assertEqual(date(2026, 8, 31), first.period_end)
        self.assertEqual("channel", first.dim_scope)
        self.assertEqual("天猫", first.dimension_value)
        self.assertEqual("revenue", first.metric)
        self.assertEqual(Decimal("1000"), first.value)
        self.assertEqual("元", first.unit)
        self.assertEqual("run-1", first.source_run_id)

    def test_replacing_facts_deletes_the_period_first(self):
        connection = FakeConnection()
        facts = (
            FactRow(
                dataset="ecommerce_monthly", period_type="month",
                period_start=date(2026, 8, 1), period_end=date(2026, 8, 31),
                dim_scope="channel", dimension_value="天猫", metric="revenue",
                value=Decimal("1"), unit="元", source_run_id="run-1",
            ),
        )

        written = ManualProjector(connection).replace_facts(
            facts, synced_at="2026-09-15 10:00:00"
        )

        self.assertEqual(1, written)
        events = connection.events
        self.assertIn("DELETE FROM `fact_manual_report`", events[0][1])
        self.assertEqual(
            ("ecommerce_monthly", "month", date(2026, 8, 1)), tuple(events[0][2])
        )
        self.assertIn("INSERT INTO `fact_manual_report`", events[1][1])
        self.assertIn("ON DUPLICATE KEY UPDATE", events[1][1])

    def test_a_second_projection_replaces_rather_than_appends(self):
        connection = FakeConnection()
        projector = ManualProjector(connection)
        facts = self._build_two_versions()

        projector.replace_facts(facts[:1], synced_at="t1")
        projector.replace_facts(facts[1:], synced_at="t2")

        deletes = [e for e in connection.events if e[1].startswith("DELETE")]
        self.assertEqual(2, len(deletes))
        inserted = [e for e in connection.events if e[1].startswith("INSERT")]
        self.assertEqual(2, len(inserted))
        self.assertEqual("run-2", inserted[-1][2][9])

    def _build_two_versions(self):
        return (
            FactRow(
                dataset="ecommerce_monthly", period_type="month",
                period_start=date(2026, 8, 1), period_end=date(2026, 8, 31),
                dim_scope="channel", dimension_value="天猫", metric="revenue",
                value=Decimal("1"), unit="元", source_run_id="run-1",
            ),
            FactRow(
                dataset="ecommerce_monthly", period_type="month",
                period_start=date(2026, 8, 1), period_end=date(2026, 8, 31),
                dim_scope="channel", dimension_value="天猫", metric="revenue",
                value=Decimal("2"), unit="元", source_run_id="run-2",
            ),
        )

    def test_empty_facts_write_nothing(self):
        connection = FakeConnection()

        self.assertEqual(0, ManualProjector(connection).replace_facts((), synced_at="t"))
        self.assertEqual([], connection.events)


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

class ServiceTests(ManualImportTestCase):
    def setUp(self):
        super().setUp()
        self.repository = InMemoryRepository()
        self.projector = InMemoryProjector()
        self.run_ids = iter(["run-1", "run-2", "run-3"])
        self.service = ManualImportService(
            repository=self.repository,
            projector=self.projector,
            now=lambda: "2026-09-15 10:00:00",
            new_run_id=lambda: next(self.run_ids),
        )

    def table(self, csv_text=ECOMMERCE_CSV, name="report.csv"):
        return load_table(self.write_csv(csv_text, name=name))

    def test_apply_persists_rows_and_projects_facts(self):
        result = self.service.apply(self.template(), self.table(), "2026-08")

        self.assertEqual(STATUS_IMPORTED, result.status)
        self.assertEqual(2, result.rows_ok)
        self.assertEqual("run-1", result.run_id)
        self.assertEqual(2, len(self.repository.rows))
        self.assertEqual(1, len(self.projector.calls))
        # 2 个明细行 × 3 个指标 = 6 行窄表（合计行不导入）
        self.assertEqual(6, len(self.projector.calls[0]))
        self.assertEqual("imported", self.repository.runs["run-1"]["status"])

    def test_reimporting_the_same_file_is_a_no_op(self):
        table = self.table()
        first = self.service.apply(self.template(), table, "2026-08")
        second = self.service.apply(self.template(), table, "2026-08")

        self.assertEqual(STATUS_IMPORTED, first.status)
        self.assertEqual(STATUS_DUPLICATE, second.status)
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(1, len(self.projector.calls))
        self.assertEqual(["run-1"], list(self.repository.runs))

    def test_a_changed_file_for_the_same_period_overwrites(self):
        self.service.apply(self.template(), self.table(), "2026-08")
        changed = self.table(
            "月份,渠道,销售收入,毛利,费用\n"
            "2026-08,天猫,1500,400,50\n"
            "2026-08,合计,1500,400,50\n",
            name="changed.csv",
        )

        result = self.service.apply(self.template(), changed, "2026-08")

        self.assertEqual(STATUS_IMPORTED, result.status)
        self.assertEqual("run-2", result.run_id)
        self.assertEqual(1, len(self.repository.rows))  # 覆盖而非追加
        self.assertEqual(2, len(self.projector.calls))
        self.assertEqual("天猫", self.repository.rows[0][3].fields["channel"])

    def test_invalid_file_is_rejected_without_writing_facts(self):
        bad = self.table(
            "月份,渠道,销售收入,毛利,费用\n2026-08,天猫,abc,300,50\n"
        )

        result = self.service.apply(self.template(), bad, "2026-08")

        self.assertEqual(STATUS_REJECTED, result.status)
        self.assertEqual(0, result.rows_ok)
        self.assertEqual([], self.projector.calls)
        self.assertEqual([], self.repository.rows)
        self.assertEqual("rejected", self.repository.runs["run-1"]["status"])

    def test_dry_run_writes_nothing(self):
        result = self.service.dry_run(self.template(), self.table(), "2026-08")

        self.assertEqual(STATUS_DRY_RUN, result.status)
        self.assertEqual("-", result.run_id)
        self.assertFalse(self.repository.runs)
        self.assertEqual([], self.projector.calls)

    def test_dry_run_reports_errors_as_rejected(self):
        bad = self.table("月份,渠道,销售收入,毛利,费用\n2026-08,天猫,abc,300,50\n")

        result = self.service.dry_run(self.template(), bad, "2026-08")

        self.assertEqual(STATUS_REJECTED, result.status)
        self.assertFalse(result.report.ok)

    def test_service_without_storage_cannot_apply(self):
        with self.assertRaises(RuntimeError):
            ManualImportService().apply(self.template(), self.table(), "2026-08")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class ImportManualCliTests(ManualImportTestCase):
    def run_cli(self, argv):
        output = io.StringIO()
        with redirect_stdout(output):
            main(argv)
        return output.getvalue()

    def test_dry_run_prints_the_report_without_touching_settings(self):
        path = self.write_csv(ECOMMERCE_CSV)

        with patch("common.public_data.cli.load_settings") as load_settings:
            text = self.run_cli([
                "import-manual",
                "--template", "ecommerce_monthly",
                "--file", str(path),
                "--period", "2026-08",
                "--templates-dir", str(TEMPLATE_DIR),
            ])

        load_settings.assert_not_called()
        self.assertIn("dataset=ecommerce_monthly", text)
        self.assertIn("rows_ok=2", text)
        self.assertIn("status=ok", text)
        self.assertIn("status=dry_run", text)

    def test_dry_run_exits_nonzero_when_the_file_has_errors(self):
        path = self.write_csv(
            "月份,渠道,销售收入,毛利,费用\n2026-08,天猫,abc,300,50\n"
        )

        with self.assertRaises(SystemExit) as raised:
            self.run_cli([
                "import-manual",
                "--template", "ecommerce_monthly",
                "--file", str(path),
                "--period", "2026-08",
                "--templates-dir", str(TEMPLATE_DIR),
            ])

        self.assertNotEqual(0, raised.exception.code or 0)

    def test_apply_uses_the_service_and_prints_the_run_summary(self):
        path = self.write_csv(ECOMMERCE_CSV)
        result = ManualImportResult(
            run_id="run-9",
            dataset="ecommerce_monthly",
            period="2026-08",
            rows_ok=2,
            rows_bad=0,
            status=STATUS_IMPORTED,
            report=ValidationReport(
                dataset="ecommerce_monthly",
                template_version=1,
                period="2026-08",
                period_start=date(2026, 8, 1),
                period_end=date(2026, 8, 31),
                total_rows=3,
            ),
        )
        with patch("common.public_data.cli.load_settings") as load_settings, \
             patch("common.public_data.cli.build_manual_import_service") as build:
            build.return_value.apply.return_value = result
            text = self.run_cli([
                "import-manual",
                "--template", "ecommerce_monthly",
                "--file", str(path),
                "--period", "2026-08",
                "--apply",
                "--imported-by", "alice",
                "--templates-dir", str(TEMPLATE_DIR),
            ])

        load_settings.assert_called_once_with()
        build.return_value.apply.assert_called_once()
        call = build.return_value.apply.call_args
        self.assertEqual("alice", call.kwargs["imported_by"])
        self.assertEqual("2026-08", call.args[2])
        self.assertIn("run_id=run-9", text)
        self.assertIn("status=imported", text)

    def test_unreadable_file_fails_without_a_traceback(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_cli([
                "import-manual",
                "--template", "ecommerce_monthly",
                "--file", str(self.tmp / "missing.csv"),
                "--period", "2026-08",
                "--templates-dir", str(TEMPLATE_DIR),
            ])

        self.assertNotEqual(0, raised.exception.code or 0)

    def test_failure_output_is_safe(self):
        output = io.StringIO()
        with patch("common.public_data.cli.load_manual_template",
                   side_effect=RuntimeError("secret-host")):
            with redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    main([
                        "import-manual",
                        "--template", "ecommerce_monthly",
                        "--file", "x.csv",
                        "--period", "2026-08",
                    ])

        text = output.getvalue()
        self.assertIn("status=failed", text)
        self.assertNotIn("secret-host", text)
        self.assertNotIn("Traceback", text)


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------

class ManualMigrationTests(unittest.TestCase):
    def test_manual_versions_are_registered(self):
        versions = [version for version, _, _ in _MIGRATIONS]

        self.assertIn("raw-manual-v1", versions)
        self.assertIn("mart-ops-manual-report-v1", versions)

    def test_migrate_builds_manual_tables_on_the_manual_database(self):
        dingtalk, wdt, mart, manual = (
            FakeConnection(), FakeConnection(), FakeConnection(), FakeConnection()
        )

        apply_live_migrations(dingtalk, wdt, mart, manual_connection=manual)

        manual_sql = manual.sql_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS `manual_import_runs`", manual_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `manual_import_row`", manual_sql)
        self.assertIn("uk_dataset_period_file", manual_sql)
        self.assertNotIn("manual_import_runs", dingtalk.sql_text())
        self.assertNotIn("manual_import_runs", wdt.sql_text())
        self.assertNotIn("manual_import_runs", mart.sql_text())

    def test_migrate_builds_the_mart_fact_table(self):
        mart, manual = FakeConnection(), FakeConnection()

        apply_live_migrations(
            FakeConnection(), FakeConnection(), mart, manual_connection=manual
        )

        mart_sql = mart.sql_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS `fact_manual_report`", mart_sql)
        self.assertIn("`period_type` ENUM('month', 'quarter', 'year') NOT NULL", mart_sql)
        self.assertIn("`source_run_id` CHAR(36) NOT NULL", mart_sql)

    def test_manual_tables_need_an_explicit_connection(self):
        mart = FakeConnection()

        apply_live_migrations(FakeConnection(), FakeConnection(), mart)

        self.assertNotIn("manual_import_runs", mart.sql_text())

    def test_channel_migrations_apply_only_their_own_versions(self):
        manual, mart = FakeConnection(), FakeConnection()

        apply_manual_migrations(manual, mart)

        self.assertIn("manual_import_runs", manual.sql_text())
        self.assertIn("fact_manual_report", mart.sql_text())
        self.assertNotIn("fact_daily_report_offline", mart.sql_text())
        self.assertNotIn("wdt_records", manual.sql_text())

    def test_applied_manual_migration_is_not_replayed(self):
        applied = {
            version: "0" * 64
            for version, _, _ in _MIGRATIONS
        }
        manual, mart = FakeConnection(), FakeConnection()

        # 已应用但校验和不同 → 漂移，必须拒绝（与既有迁移同语义）
        from common.public_data.live_migrations import LiveMigrationError
        with self.assertRaises(LiveMigrationError):
            apply_manual_migrations(manual, mart, applied_checksums=applied)


if __name__ == "__main__":
    unittest.main()
