# -*- coding: utf-8 -*-
"""Agent Skills：声明式注册表（flat 模式默认）。"""

from common.kb_qa.skills.registry import (  # noqa: F401
    SkillDef,
    SkillRegistryError,
    business_tool_names,
    compile_tools,
    export_tools,
    get_skill,
    load_registry,
    validate_handlers,
)
