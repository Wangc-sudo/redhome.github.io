"""人工报表文件读取：CSV / XLSX → 与模板无关的「原始行」。

loader 只做一件事：**把文件读成行**，不做类型解释、不做业务校验。
表头原样保留（人工报表表头带空格、换行、全角括号是常态），映射到标准
字段是 validate 的事；这样同一份文件换模板重导时无需重新解析文件。

行号从 **2** 开始（第 1 行是表头），与 Excel 一致，校验报告里报的行号
业务能直接在原表里定位。

XLSX 依赖 openpyxl（惰性导入）：容器里没装时给出明确报错，而不是在
import 阶段炸掉整个 CLI。
"""

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

#: 支持的文件后缀（小写比较）。
SUPPORTED_SUFFIXES = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm")


class ManualImportError(RuntimeError):
    """文件无法读取（消息只含文件名与原因，不含单元格内容）。"""


@dataclass(frozen=True)
class SourceRow:
    """一行原始数据：行号 + 「源列名 → 原样值」。"""

    row_no: int
    values: dict


@dataclass(frozen=True)
class LoadedTable:
    """一次文件读取的结果。

    ``sha256`` 是文件字节的摘要，承担**同文件重复导入幂等**的身份：
    内容换一个字节就是新一次导入，同一字节重跑则无副作用。
    """

    path: str
    file_name: str
    header: tuple[str, ...]
    rows: tuple[SourceRow, ...]
    sha256: str


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    target = Path(path)
    try:
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ManualImportError("file is not readable") from exc
    return digest.hexdigest()


def load_table(path, sheet=None) -> LoadedTable:
    """读取 *path*，返回 :class:`LoadedTable`。"""
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ManualImportError(
            f"unsupported file type: {suffix or '(none)'} "
            f"(supported: {', '.join(SUPPORTED_SUFFIXES)})"
        )
    if not target.is_file():
        raise ManualImportError("file does not exist")

    if suffix in (".xlsx", ".xlsm"):
        header, rows = _read_xlsx(target, sheet)
    else:
        header, rows = _read_delimited(target, suffix)

    return LoadedTable(
        path=str(target),
        file_name=target.name,
        header=header,
        rows=rows,
        sha256=file_sha256(target),
    )


def missing_source_columns(table: LoadedTable, template) -> tuple[str, ...]:
    """模板声明了、但文件里没有的源列（顺序按模板）。"""
    present = set(table.header)
    return tuple(
        column.source for column in template.columns
        if column.source not in present
    )


# ---------------------------------------------------------------------------
# 内部读取
# ---------------------------------------------------------------------------

def _clean_header(values) -> tuple[str, ...]:
    header = []
    for value in values:
        text = "" if value is None else str(value).strip()
        header.append(text)
    # 去掉右侧的空表头（Excel 常见：多余列没有表头）
    while header and not header[-1]:
        header.pop()
    return tuple(header)


def _read_delimited(target: Path, suffix: str):
    delimiter = "\t" if suffix == ".tsv" else ","
    try:
        with target.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            raw_rows = list(reader)
    except OSError as exc:
        raise ManualImportError("file is not readable") from exc
    except UnicodeDecodeError as exc:
        raise ManualImportError("file is not valid UTF-8 text") from exc

    if not raw_rows:
        raise ManualImportError("file is empty")

    header = _clean_header(raw_rows[0])
    rows = []
    for index, raw in enumerate(raw_rows[1:], start=2):
        if not any("" if cell is None else str(cell).strip() for cell in raw):
            continue  # 空行：报表里的分隔空行，不算数据行
        values = {}
        for position, name in enumerate(header):
            values[name] = raw[position] if position < len(raw) else None
        rows.append(SourceRow(row_no=index, values=values))
    return header, tuple(rows)


def _read_xlsx(target: Path, sheet=None):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise ManualImportError(
            "reading .xlsx requires openpyxl, which is not installed"
        ) from exc

    try:
        workbook = load_workbook(target, data_only=True, read_only=True)
    except Exception as exc:
        raise ManualImportError("workbook could not be opened") from exc

    try:
        worksheet = workbook[sheet] if sheet else workbook.worksheets[0]
        raw_rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
    except (KeyError, IndexError, AttributeError) as exc:
        raise ManualImportError("worksheet could not be read") from exc
    finally:
        workbook.close()

    while raw_rows and not any(
        "" if cell is None else str(cell).strip() for cell in raw_rows[0]
    ):
        raw_rows.pop(0)  # 报表顶部的标题/说明行，表头之前整行跳过
    if not raw_rows:
        raise ManualImportError("file is empty")

    header = _clean_header(raw_rows[0])
    rows = []
    for index, raw in enumerate(raw_rows[1:], start=2):
        if not any("" if cell is None else str(cell).strip() for cell in raw):
            continue
        values = {}
        for position, name in enumerate(header):
            values[name] = raw[position] if position < len(raw) else None
        rows.append(SourceRow(row_no=index, values=values))
    return header, tuple(rows)
