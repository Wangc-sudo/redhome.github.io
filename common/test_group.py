# -*- coding: utf-8 -*-
"""钉钉测试群统一配置选择器。

环境变量 TEST_MODE=1 时，把机器人的目标群切换到 common/test_groups.json 里的 default 群。
"""
import json
import os
from pathlib import Path

_DEFAULT_PATH = Path(__file__).with_name("test_groups.json")


def load_test_groups(path=None):
    """读取测试群配置。path 默认指向本模块同目录下的 test_groups.json。"""
    p = Path(path) if path else _DEFAULT_PATH
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"找不到测试群配置: {p}") from None
    except json.JSONDecodeError as e:
        raise RuntimeError(f"测试群配置 JSON 解析失败: {e}") from e


def resolve_target(robot_config, mode=None):
    """根据 mode 或 TEST_MODE 环境变量返回目标群配置。

    :param robot_config: 正式群配置，通常来自 CONFIG["robot"] 或 CONFIG["push"]。
    :param mode: None 时读取环境变量；"test" 强制测试群；其他值使用正式群。
    :return: 新的配置字典；非测试模式下直接返回原配置。
    """
    if mode is None:
        mode = "test" if os.environ.get("TEST_MODE") == "1" else "prod"
    if mode != "test":
        return robot_config

    groups = load_test_groups()
    test = groups.get("default")
    if not test:
        raise RuntimeError("common/test_groups.json 缺少 default 节点")

    target = dict(robot_config)
    if "webhook" in test:
        target["mode"] = "webhook"
        target["webhook"] = test["webhook"]
        target["secret"] = test.get("secret", "")
        target.pop("openConversationId", None)
    elif "openConversationId" in test:
        target["mode"] = "groupSend"
        target["openConversationId"] = test["openConversationId"]
        if "robotCode" in test:
            target["robotCode"] = test["robotCode"]
        if "groupName" in test:
            target["groupName"] = test["groupName"]
        target.pop("webhook", None)
        target.pop("secret", None)
    else:
        raise RuntimeError(
            "common/test_groups.json/default 缺少 webhook 或 openConversationId"
        )

    return target
