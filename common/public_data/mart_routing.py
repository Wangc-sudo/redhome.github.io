"""mart 拆库（mart_facts / mart_dims / mart_queue）的连接路由与灰度开关。

设计稿：``docs/superpowers/specs/2026-09-16-mart-split-design.md``（§2.2/§2.5）。

* 三个新库在 ``Settings`` 上是可选字段；未配置时 ``resolve_*`` 一律回落
  ``settings.mart_database``——灰度期零配置即旧行为，业务代码只依赖本模块，
  不感知开关细节。
* 四个开关全部走环境变量（与全仓 env 驱动风格一致），默认值都是「旧面」：

  ======================  =================  ============================
  开关                    取值（默认加粗）   语义
  ======================  =================  ============================
  MART_SPLIT_FACTS_WRITE  **off**/dual/new   fact 写面：只写旧/双写/只写新
  MART_SPLIT_FACTS_READ   **old**/new        fact 读面
  MART_SPLIT_DIMS_READ    **old**/new        dims 读面（dims 单写，无写开关）
  MART_SPLIT_QUEUE        **off**/on         outbox 指向 mart_queue
  ======================  =================  ============================

注意与设计稿 §2.2 的一处刻意偏差：设计表中「缺省推导 mart_facts[_test]」
不落在 Settings 字段上——若缺省也落成具体库名，「未配 → 回落旧库」的灰度
语义就无从判断。推荐值不变：运维启用时显式配 ``mart_facts[_test]`` /
``mart_dims[_test]`` / ``mart_queue[_test]`` 即可。
"""

import os
from dataclasses import dataclass

from common.public_data.settings import DatabaseSettings, Settings

FACTS_WRITE_ENV = "MART_SPLIT_FACTS_WRITE"
FACTS_READ_ENV = "MART_SPLIT_FACTS_READ"
DIMS_READ_ENV = "MART_SPLIT_DIMS_READ"
QUEUE_ENV = "MART_SPLIT_QUEUE"

_FACTS_WRITE_VALUES = ("off", "dual", "new")
_READ_VALUES = ("old", "new")
_QUEUE_VALUES = ("off", "on")


class MartRoutingError(ValueError):
    """开关取值非法（拼写错误、未支持的灰度态）。"""


def _parse_switch(environ, name, allowed, default):
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    value = raw.lower()
    if value not in allowed:
        raise MartRoutingError(
            f"{name} must be one of {allowed}, got {raw!r}"
        )
    return value


@dataclass(frozen=True)
class MartSplitSwitches:
    """四个拆库灰度开关的解析结果；缺省全旧面，等价于未拆库。"""

    facts_write: str = "off"
    facts_read: str = "old"
    dims_read: str = "old"
    queue: str = "off"

    @classmethod
    def from_environment(cls, environ=None):
        environment = os.environ if environ is None else environ
        return cls(
            facts_write=_parse_switch(
                environment, FACTS_WRITE_ENV, _FACTS_WRITE_VALUES, "off"
            ),
            facts_read=_parse_switch(
                environment, FACTS_READ_ENV, _READ_VALUES, "old"
            ),
            dims_read=_parse_switch(
                environment, DIMS_READ_ENV, _READ_VALUES, "old"
            ),
            queue=_parse_switch(environment, QUEUE_ENV, _QUEUE_VALUES, "off"),
        )


def resolve_facts_database(settings: Settings) -> DatabaseSettings:
    """mart_facts 连接配置；未配置时回落旧 mart 库（零拷贝，frozen 复用）。"""
    return settings.mart_facts_database or settings.mart_database


def resolve_dims_database(settings: Settings) -> DatabaseSettings:
    return settings.mart_dims_database or settings.mart_database


def resolve_queue_database(settings: Settings) -> DatabaseSettings:
    return settings.mart_queue_database or settings.mart_database


def facts_write_databases(
    settings: Settings, switches: MartSplitSwitches
) -> tuple[DatabaseSettings, ...]:
    """extract / gateway 报数的 fact 写入面（按写入顺序）。

    * ``off``  → 只写旧库；
    * ``dual`` → 旧库 + 新库（新库未配置时回落同库，自动去重为单写）；
    * ``new``  → 只写新库。
    """
    old = settings.mart_database
    new = resolve_facts_database(settings)
    if switches.facts_write == "dual":
        return (old,) if new.name == old.name else (old, new)
    if switches.facts_write == "new":
        return (new,)
    return (old,)


def facts_read_database(
    settings: Settings, switches: MartSplitSwitches
) -> DatabaseSettings:
    """bi-web / robot / pages 的 fact 读取面。"""
    if switches.facts_read == "new":
        return resolve_facts_database(settings)
    return settings.mart_database


def dims_read_database(
    settings: Settings, switches: MartSplitSwitches
) -> DatabaseSettings:
    """dims 读取面（dims 单写入者，RENAME 后只需切读，无写开关）。"""
    if switches.dims_read == "new":
        return resolve_dims_database(settings)
    return settings.mart_database


def queue_database(
    settings: Settings, switches: MartSplitSwitches
) -> DatabaseSettings:
    """robot 入队与 gateway 轮询的 outbox 归属。"""
    if switches.queue == "on":
        return resolve_queue_database(settings)
    return settings.mart_database


def qualified_table(database: DatabaseSettings, table: str) -> str:
    """同实例跨 schema 限定名（如 ``mart_dims`.`dim_robot_member``）。

    供 ``fetch_unfilled_members`` 一类跨 dim×fact 的单条 SQL JOIN 在切读后
    继续成立（设计稿 §1.2 约束 2 / §2.5 改动面）。
    """
    return f"`{database.name}`.`{table}`"
