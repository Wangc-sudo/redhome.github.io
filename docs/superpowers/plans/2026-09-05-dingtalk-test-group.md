# DingTalk 测试群统一配置实现计划

> **For agentic workers:** REQUIRED SUB-_SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 digital-ops 所有钉钉机器人添加统一的测试群切换能力：通过 `TEST_MODE=1` 环境变量把消息重定向到「功能验证群」。

**Architecture：** 新增 `common/test_groups.json`（本地配置，不入仓）和 `common/test_group.py` 辅助模块，提供 `resolve_target()` 在正式配置与测试群配置之间选择；在杭州日报机器人和渠道日报机器人的发送点各包一层 `resolve_target`。

**Tech Stack：** Python 3.12，标准库 `unittest`/`pathlib`/`os`，不引入新依赖。

---

## 文件清单

| 文件 | 操作 | 说明 |
|---|---|---|
| `common/test_groups.example.json` | 创建 | 测试群配置模板 |
| `.gitignore` | 修改 | 忽略 `common/test_groups.json` |
| `common/test_group.py` | 创建 | 核心辅助模块 |
| `tests/common/test_test_group.py` | 创建 | 单元测试 |
| `数字化/钉钉/杭州日报机器人/hangzhou_reminder.py` | 修改 | 企业机器人发送点接入 |
| `数字化/钉钉/渠道日报机器人/main.py` | 修改 | webhook/groupSend 发送点接入 |

---

### Task 1: 配置模板与 gitignore

**Files:**
- Create: `common/test_groups.example.json`
- Modify: `.gitignore`

- [ ] **Step 1: 创建配置模板**

创建 `common/test_groups.example.json`：

```json
{
  "default": {
    "groupName": "功能验证群",
    "groupNumber": "197925004762",
    "openConversationId": "<测试群 openConversationId>"
  }
}
```

- [ ] **Step 2: 加入 gitignore**

在 `.gitignore` 末尾新增一行：

```gitignore
# 测试群真实配置（含 openConversationId）
common/test_groups.json
```

- [ ] **Step 3: Commit**

```bash
git add common/test_groups.example.json .gitignore
git commit -m "chore: add test group config template and ignore real config"
```

---

### Task 2: 为默认行为写失败测试

**Files:**
- Create: `tests/common/test_test_group.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/common/test_test_group.py`：

```python
import os
import unittest
from pathlib import Path

# 把 repo 根目录加入路径，确保能 import common
REPO_ROOT = Path(__file__).resolve().parents[2]
import sys
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common import test_group


class TestResolveTarget(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("TEST_MODE", None)

    def test_prod_mode_returns_original_config(self):
        os.environ.pop("TEST_MODE", None)
        original = {"robotCode": "rc", "openConversationId": "prod-cid"}
        # 通过注入测试配置绕过真实文件
        test_group.load_test_groups = lambda path=None: {
            "default": {
                "groupName": "功能验证群",
                "groupNumber": "197925004762",
                "openConversationId": "test-cid",
            }
        }
        result = test_group.resolve_target(original)
        self.assertEqual(result, original)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
python -m unittest tests.common.test_test_group -v
```

Expected: `ModuleNotFoundError: No module named 'common.test_group'`

---

### Task 3: 实现 `common/test_group.py`

**Files:**
- Create: `common/test_group.py`

- [ ] **Step 1: 实现模块**

创建 `common/test_group.py`：

```python
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
        if "groupName" in test:
            target["groupName"] = test["groupName"]
        target.pop("webhook", None)
        target.pop("secret", None)
    else:
        raise RuntimeError(
            "common/test_groups.json/default 缺少 webhook 或 openConversationId"
        )

    return target
```

- [ ] **Step 2: 运行测试，确认通过**

```bash
python -m unittest tests.common.test_test_group -v
```

Expected: `test_prod_mode_returns_original_config ... ok`

- [ ] **Step 3: Commit**

```bash
git add common/test_group.py tests/common/test_test_group.py
git commit -m "feat: add test group resolver with prod-mode test"
```

---

### Task 4: 补充测试模式测试

**Files:**
- Modify: `tests/common/test_test_group.py`

- [ ] **Step 1: 添加测试用例**

在 `TestResolveTarget` 中追加：

```python
    def test_test_mode_overrides_open_conversation_id(self):
        os.environ["TEST_MODE"] = "1"
        original = {"robotCode": "rc", "openConversationId": "prod-cid"}
        test_group.load_test_groups = lambda path=None: {
            "default": {
                "groupName": "功能验证群",
                "groupNumber": "197925004762",
                "openConversationId": "test-cid",
            }
        }
        result = test_group.resolve_target(original)
        self.assertEqual(result["robotCode"], "rc")
        self.assertEqual(result["openConversationId"], "test-cid")
        self.assertEqual(result["groupName"], "功能验证群")
        self.assertEqual(result["mode"], "groupSend")

    def test_explicit_mode_overrides_env(self):
        os.environ["TEST_MODE"] = "1"
        original = {"robotCode": "rc", "openConversationId": "prod-cid"}
        test_group.load_test_groups = lambda path=None: {
            "default": {"openConversationId": "test-cid"}
        }
        result = test_group.resolve_target(original, mode="prod")
        self.assertEqual(result, original)

    def test_webhook_test_group(self):
        os.environ["TEST_MODE"] = "1"
        original = {
            "mode": "webhook",
            "webhook": "https://prod.example.com/webhook",
            "secret": "prod-secret",
        }
        test_group.load_test_groups = lambda path=None: {
            "default": {
                "groupName": "功能验证群",
                "webhook": "https://test.example.com/webhook",
                "secret": "test-secret",
            }
        }
        result = test_group.resolve_target(original)
        self.assertEqual(result["mode"], "webhook")
        self.assertEqual(result["webhook"], "https://test.example.com/webhook")
        self.assertEqual(result["secret"], "test-secret")
        self.assertNotIn("openConversationId", result)
```

- [ ] **Step 2: 运行全部测试**

```bash
python -m unittest tests.common.test_test_group -v
```

Expected: 4 tests pass

- [ ] **Step 3: Commit**

```bash
git add tests/common/test_test_group.py
git commit -m "test: add test mode and webhook test group cases"
```

---

### Task 5: 杭州日报机器人接入测试群切换

**Files:**
- Modify: `数字化/钉钉/杭州日报机器人/hangzhou_reminder.py:100-106`

- [ ] **Step 1: 导入辅助函数**

在 `数字化/钉钉/杭州日报机器人/hangzhou_reminder.py` 中，找到第 25 行附近的导入区，在 `from common.dingtalk import DingTalkClient` 下一行新增：

```python
from common.test_group import resolve_target
```

- [ ] **Step 2: 修改 send_group**

把第 100-106 行的 `send_group` 函数改成：

```python
def send_group(title, text, at_ids=None):
    robot = resolve_target(CONFIG["robot"])
    client = DingTalk(CONFIG["dingtalk"])
    r = client.send_group_markdown(robot["robotCode"], robot["openConversationId"],
                                   title, text, at_user_ids=at_ids)
    log(f"已发群消息: {title} (at={at_ids}) -> {robot.get('groupName', 'unknown')}")
    return r
```

- [ ] **Step 3: 编译检查**

```bash
python -m compileall -q "数字化/钉钉/杭州日报机器人/hangzhou_reminder.py"
```

Expected: `Compiled 1 files.` 或无输出

- [ ] **Step 4: Commit**

```bash
git add "数字化/钉钉/杭州日报机器人/hangzhou_reminder.py"
git commit -m "feat(hangzhou): switch target group via TEST_MODE"
```

---

### Task 6: 渠道日报机器人接入测试群切换

**Files:**
- Modify: `数字化/钉钉/渠道日报机器人/main.py:147-161`

- [ ] **Step 1: 导入辅助函数**

在 `数字化/钉钉/渠道日报机器人/main.py` 顶部导入区新增：

```python
from common.test_group import resolve_target
```

- [ ] **Step 2: 修改发送逻辑**

把第 147-161 行改成：

```python
        push = resolve_target(cfg["push"])
        if push.get("mode") == "webhook":
            client.send_webhook_markdown(
                push["webhook"],
                title=f"渠道日报 {target_str}",
                markdown_text=markdown,
                secret=push.get("secret", ""),
            )
            log(f"webhook 推送成功 -> {push.get('groupName', push.get('webhook'))}")
        else:
            client.send_group_message(
                push["robotCode"], push["openConversationId"],
                "sampleMarkdown", {"title": f"渠道日报 {target_str}", "text": markdown},
            )
            log(f"群消息推送成功 -> {push.get('groupName', 'unknown')}")
```

- [ ] **Step 3: 编译检查**

```bash
python -m compileall -q "数字化/钉钉/渠道日报机器人/main.py"
```

Expected: 无错误

- [ ] **Step 4: Commit**

```bash
git add "数字化/钉钉/渠道日报机器人/main.py"
git commit -m "feat(channel): switch target group via TEST_MODE"
```

---

### Task 7: 全仓库编译与测试回归

**Files:**
- None

- [ ] **Step 1: 运行单元测试**

```bash
python -m unittest discover tests -v
```

Expected: `test_explicit_mode_overrides_env ... ok` 等 4 个测试通过

- [ ] **Step 2: 全仓库编译检查**

```bash
python -m compileall -q "数字化" "common"
```

Expected: 无输出或 `Compiled N files.`

- [ ] **Step 3: Commit（如仅有计划文档未提交）**

```bash
git add docs/superpowers/plans/2026-09-05-dingtalk-test-group.md
git commit -m "docs: add test group implementation plan"
```

---

## 验证清单

- [ ] `TEST_MODE` 未设置时，杭州日报机器人仍发到「线下事业部杭州日报群」。
- [ ] `TEST_MODE=1` 时，杭州日报机器人发到「功能验证群」。
- [ ] `TEST_MODE=1` 时，渠道日报机器人根据 `test_groups.json` 发到测试群（webhook 或 groupSend）。
- [ ] 单元测试全部通过。
- [ ] 全仓库 `compileall` 通过。
