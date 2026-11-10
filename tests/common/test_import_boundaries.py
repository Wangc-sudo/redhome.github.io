"""轻量 import 边界门禁：既有违规只减不增，新拆分模块不得成环。"""

import ast
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_UPPER_LAYERS = ("bi_web", "ops_web", "kb_qa", "rag", "daily_robot")

# 既有反向依赖按「文件 + 目标包」登记，允许原样保留但不允许新增。
# live_migrations 是集中迁移注册表；其余为延迟加载的跨域组合入口。
_EXISTING_REVERSE_IMPORTS = {
    ("common/public_data/cli.py", "bi_web"): "CLI 延迟装配 BI seed 发布入口",
    ("common/public_data/cli.py", "kb_qa"): "CLI 延迟装配知识库导出入口",
    ("common/public_data/live_migrations.py", "kb_qa"): "集中迁移注册表引用 schema",
    ("common/public_data/live_migrations.py", "rag"): "集中迁移注册表引用索引状态 schema",
    ("common/public_data/recipient_override.py", "daily_robot"): "收件人覆盖延迟解析 @ 用户",
    ("common/public_data/report_roster.py", "daily_robot"): "名册解析延迟复用门店负责人解析器",
    ("common/rag/index_products.py", "kb_qa"): "商品索引进程复用附件下载与记录归一化",
}


def _module_name(node):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:
            return []
        return [node.module or ""]
    return []


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        for module in _module_name(node):
            yield node.lineno, module


def _target_package(module):
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "common":
        return parts[1]
    return None


class ImportBoundaryTests(unittest.TestCase):
    def test_no_new_reverse_dependencies(self):
        rules = [("common/public_data", _UPPER_LAYERS), ("common/rag", ("kb_qa",))]
        connector_roots = (
            "common/dingtalk",
            "common/wdt",
            "common/connectors",
            "common/platform",
            "common/public_data/connectors",
        )
        rules.extend((root, _UPPER_LAYERS) for root in connector_roots)

        offenders = []
        for root_name, forbidden in rules:
            root = _REPO_ROOT / root_name
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.py")):
                relative = path.relative_to(_REPO_ROOT).as_posix()
                for line, module in _imports(path):
                    target = _target_package(module)
                    if target not in forbidden:
                        continue
                    baseline = _EXISTING_REVERSE_IMPORTS.get((relative, target))
                    if baseline is None:
                        offenders.append(f"{relative}:{line}: {module}")

        self.assertEqual([], offenders, "新增反向依赖：\n" + "\n".join(offenders))

    def test_new_query_and_route_modules_do_not_import_facades_or_form_cycles(self):
        roots = (
            _REPO_ROOT / "common/bi_web/query_domains",
            _REPO_ROOT / "common/ops_web/routes",
        )
        for root in roots:
            if not root.exists():
                continue
            graph = {}
            for path in sorted(root.glob("*.py")):
                module = f"common.{root.parent.relative_to(_REPO_ROOT / 'common').as_posix()}.{path.stem}"
                imports = set()
                for _, imported in _imports(path):
                    if imported.startswith(module.rsplit(".", 1)[0] + "."):
                        imports.add(imported)
                    self.assertNotEqual(
                        "common.bi_web.queries" if "query_domains" in root.parts else "common.ops_web.app",
                        imported,
                        f"拆分模块不得反向导入兼容 facade：{path}:{imported}",
                    )
                graph[module] = imports

            visiting = set()
            visited = set()

            def visit(module, chain):
                if module in visiting:
                    self.fail("新增循环依赖：" + " -> ".join(chain + [module]))
                if module in visited or module not in graph:
                    return
                visiting.add(module)
                for imported in graph[module]:
                    visit(imported, chain + [module])
                visiting.remove(module)
                visited.add(module)

            for module in graph:
                visit(module, [])


if __name__ == "__main__":
    unittest.main()
