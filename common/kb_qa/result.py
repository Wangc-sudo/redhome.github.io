# -*- coding: utf-8 -*-
"""统一结果模型（2026-11-02 答复架构补齐，docs/小易答复架构-2026-11-02.md）。

此前工具结果以「截断 JSON 字符串」在管道里流动，显示层只能事后解析、
猜测语义——结论/表格/引用各成补丁。本模块定义唯一的结构化入口：

    executor 结果 payload → ResultSet → 显示层（render）

契约：
- 数字只能来自 ResultSet（LLM 不得复述合计）；
- 单格标量（max_time/latest 等探索锚点）在归一化处过滤，永远进不了
  候选答案块；
- 后续 Skills Task 1 把列语义声明迁进 SKILL.md 时，本模型是挂载点。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ResultSet:
    """一次工具查询的结构化结果。"""

    tool: str
    rows: list = field(default_factory=list)
    total_rows: int = None

    @property
    def complete(self):
        """结果集完整（未被截断）——不完整时合计/结论卡会误导，不出。"""
        return self.total_rows is None or self.total_rows <= len(self.rows)


def from_payload(tool, payload):
    """工具结果 payload → ResultSet；非表格结果/错误/探索锚点返回 None。"""
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    if not all(isinstance(row, dict) for row in rows):
        return None
    if len(rows) == 1 and len(rows[0]) == 1:
        # 单格标量（max_time/latest 等日期锚点）是探索过程副产物。
        return None
    total = payload.get("total_rows", payload.get("count"))
    return ResultSet(tool=tool, rows=rows, total_rows=total)
