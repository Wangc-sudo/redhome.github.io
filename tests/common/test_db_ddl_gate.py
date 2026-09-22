"""DB 建表门禁（2026-09-17 排查报告配套制度）。

三道闸，全部失败即红，防「绕过迁移机制私自建表」：

1. **DDL 集中登记**：生产代码里的 ``CREATE/ALTER/DROP TABLE`` 只允许出现在
   白名单模块（迁移注册表与各 schema 定义模块）。任何新文件出现 DDL 文本
   都会被打回——正确姿势是把 DDL 登记进 ``live_migrations._MIGRATIONS``
   （新版本号），让建表走「版本 + 校验和 + 跟踪表」的正规通道。
2. **迁移注册纪律**：版本号命名、target 归属、唯一性；``CREATE TABLE``
   必须 ``IF NOT EXISTS``（可重放）；迁移里禁止 ``DROP TABLE``（破坏性
   变更必须走人工评审的运维脚本，不允许混进自动迁移）。
3. **新表必须有主键**：InnoDB 表无显式主键会生成隐式聚簇索引，复制与
   归档都受拖累。登记进迁移的每张表都必须声明 ``PRIMARY KEY``。
"""

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 允许出现 DDL 文本的生产代码文件（相对仓库根）。
#: 新增条目必须附理由；业务代码一律不得入列。
_DDL_WHITELIST = frozenset(
    {
        # 迁移注册表：DDL 的唯一正规通道
        "common/public_data/live_migrations.py",
        # schema 定义模块（被 live_migrations 引用）
        "common/public_data/mart_extract_schema.py",
        "common/public_data/manual_import/schema.py",
        # BI 授权 schema 定义模块（mart-ops-bi-authz-v1 的 DDL 源，
        # 同 live_migrations 引用关系；授权数据读写 SQL 也在此集中）
        "common/public_data/bi_authz.py",
        # 独立运维脚本：wdt 商品目录一次性同步，自带 dim_product DDL
        "common/public_data/product_catalog.py",
        # 独立 SQLite 榜单服务，与 RDS 体系无关
        "数字化/钉钉/榜单服务/server.py",
    }
)

#: 扫描的生产代码目录（tests / outputs / frontend / .worktrees 不在其列）。
_SCAN_ROOTS = ("common", "数字化")

_DDL_RE = re.compile(r"\b(?:CREATE|ALTER|DROP)\s+TABLE\b", re.IGNORECASE)
_VERSION_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*-v\d+$")


def _iter_production_py_files():
    for root_name in _SCAN_ROOTS:
        root = _REPO_ROOT / root_name
        for path in sorted(root.rglob("*.py")):
            if path.name.startswith("test_"):
                continue
            yield path


class DdlConcentrationGateTests(unittest.TestCase):
    """门禁一：DDL 只准出现在白名单模块。"""

    def test_no_ddl_outside_the_whitelist(self):
        offenders = []
        for path in _iter_production_py_files():
            relative = path.relative_to(_REPO_ROOT).as_posix()
            if relative in _DDL_WHITELIST:
                continue
            if _DDL_RE.search(path.read_text(encoding="utf-8")):
                offenders.append(relative)

        self.assertEqual(
            [],
            offenders,
            "以下文件出现 CREATE/ALTER/DROP TABLE，违反「DDL 集中登记」门禁。"
            "请把 DDL 登记进 live_migrations._MIGRATIONS（新版本号），"
            "而不是在业务代码里私自建表：\n" + "\n".join(offenders),
        )


class MigrationRegistryGateTests(unittest.TestCase):
    """门禁二：迁移注册的命名、归属与安全纪律。"""

    def test_versions_are_well_formed_unique_and_target_known_schemas(self):
        from common.public_data.live_migrations import _MIGRATIONS

        known_targets = {
            "dingtalk",
            "wdt",
            "mart",
            "manual",
            "mart_facts",
            "mart_dims",
            "mart_queue",
        }
        versions = [version for version, _, _ in _MIGRATIONS]

        self.assertEqual(len(versions), len(set(versions)), "迁移版本号重复")
        for version, target, _ in _MIGRATIONS:
            with self.subTest(version=version):
                self.assertRegex(version, _VERSION_RE)
                self.assertIn(target, known_targets)

    def test_create_table_is_replayable_and_drop_is_forbidden(self):
        from common.public_data.live_migrations import _MIGRATIONS

        for version, _, statements in _MIGRATIONS:
            for statement in statements:
                with self.subTest(version=version, statement=statement[:40]):
                    normalized = re.sub(r"\s+", " ", statement).upper()
                    if "CREATE TABLE" in normalized:
                        self.assertIn("CREATE TABLE IF NOT EXISTS", normalized)
                    self.assertNotIn("DROP TABLE", normalized)


class PrimaryKeyGateTests(unittest.TestCase):
    """门禁三：登记进迁移的每张表都必须声明主键。"""

    _CREATE_RE = re.compile(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`([a-z0-9_]+)`", re.IGNORECASE
    )

    def test_every_created_table_declares_a_primary_key(self):
        from common.public_data.live_migrations import _MIGRATIONS

        tables_per_target = {}
        for version, target, statements in _MIGRATIONS:
            for statement in statements:
                match = self._CREATE_RE.search(statement)
                if not match:
                    continue
                table = match.group(1)
                with self.subTest(version=version, table=table):
                    self.assertIn("PRIMARY KEY", statement.upper())
                tables_per_target.setdefault(target, []).append(table)

        # 同一 target 内不得重复建同名表（跨 target 复用同源 DDL 是允许的，
        # 如 mart_facts 复用 mart 的冻结文本）。
        for target, tables in tables_per_target.items():
            with self.subTest(target=target):
                self.assertEqual(
                    len(tables),
                    len(set(tables)),
                    f"{target} 内重复建表：{tables}",
                )


if __name__ == "__main__":
    unittest.main()
