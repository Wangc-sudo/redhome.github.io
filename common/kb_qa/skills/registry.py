# -*- coding: utf-8 -*-
"""Agent Skills 注册表（2026-11-02 标准化 Task 1，行为零变化）。

每个技能一个目录 ``skills/<name>/SKILL.md``：YAML frontmatter 承载机器
可读元数据（name/description/parameters/tags/order/enabled/snapshot/
export），正文承载口径文档（flat 模式不消费，progressive 模式按需加载）。

约束（与 2026-10-30 计划一致）：
- 工具名一个都不改——审计/导出/前端全按工具名字符串消费；
- handler 实现留在 agent.ToolExecutor，SKILL.md 只是定义与文档，
  安全护栏集中在 executor，不能被技能定义绕过；
- flat 模式：compile_tools() 产物与迁移前 TOOLS 逐字段相等
  （tests/common/fixtures/kb_tools_snapshot.json 钉住）。
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

SKILLS_DIR = Path(__file__).resolve().parent
TAGS = frozenset({"business", "knowledge", "lookup", "meta"})
_NAME_RE = re.compile(r"^[a-z_]+$")


class SkillRegistryError(ValueError):
    """技能定义非法（import 时即抛，参照 bi_web cards 注册表模式）。"""


@dataclass(frozen=True)
class SkillDef:
    name: str
    description: str
    parameters: dict
    tags: tuple = ()
    enabled: bool = True
    order: int = 100
    snapshot: str = None
    export: bool = False
    body: str = ""


def _parse_skill_md(path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise SkillRegistryError(f"{path} 缺少 frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillRegistryError(f"{path} frontmatter 未闭合")
    meta = yaml.safe_load(parts[1])
    if not isinstance(meta, dict):
        raise SkillRegistryError(f"{path} frontmatter 不是映射")
    return meta, parts[2].strip()


def _validate(meta, dirname, path):
    name = meta.get("name")
    if name != dirname or not name or not _NAME_RE.match(name):
        raise SkillRegistryError(f"{path} name 必须等于目录名（{dirname}）")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillRegistryError(f"{path} description 不能为空")
    if len(description) > 200:
        raise SkillRegistryError(f"{path} description 超 200 字")
    parameters = meta.get("parameters")
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise SkillRegistryError(f"{path} parameters 必须是 object JSON Schema")
    if "properties" in parameters and not isinstance(
            parameters["properties"], dict):
        raise SkillRegistryError(f"{path} parameters.properties 必须是映射")
    if "required" in parameters and not isinstance(
            parameters["required"], list):
        raise SkillRegistryError(f"{path} parameters.required 必须是数组")
    tags = tuple(meta.get("tags") or ())
    if not tags or any(tag not in TAGS for tag in tags):
        raise SkillRegistryError(f"{path} tags 必须取自 {sorted(TAGS)}")
    return SkillDef(
        name=name,
        description=description,
        parameters=parameters,
        tags=tags,
        enabled=bool(meta.get("enabled", True)),
        order=int(meta.get("order", 100)),
        snapshot=meta.get("snapshot"),
        export=bool(meta.get("export", False)),
    )


def load_registry(directory=None):
    """扫描 ``*/SKILL.md`` → {name: SkillDef}（按 order 稳定排序）。"""
    root = Path(directory) if directory else SKILLS_DIR
    skills = {}
    for path in sorted(root.glob("*/SKILL.md")):
        meta, body = _parse_skill_md(path)
        skill = _validate(meta, path.parent.name, path)
        if skill.name in skills:
            raise SkillRegistryError(f"技能重名: {skill.name}")
        skills[skill.name] = SkillDef(
            **{**skill.__dict__, "body": body})
    return skills


def compile_tools(enabled_only=True, directory=None):
    """注册表 → OpenAI function schema 列表（flat 模式，顺序=order 字段）。"""
    skills = sorted(load_registry(directory).values(),
                    key=lambda s: (s.order, s.name))
    return [
        {"type": "function",
         "function": {"name": s.name, "description": s.description,
                      "parameters": s.parameters}}
        for s in skills if s.enabled or not enabled_only
    ]


def validate_handlers(executor_cls, directory=None):
    """import 时校验：每个启用的技能都有 ``_tool_<name>`` handler。

    放在 agent 模块调用（注册表不反向 import executor，避免循环依赖）。
    """
    for skill in load_registry(directory).values():
        if skill.enabled and not hasattr(executor_cls, f"_tool_{skill.name}"):
            raise SkillRegistryError(
                f"技能 {skill.name} 缺少 handler _tool_{skill.name}")
