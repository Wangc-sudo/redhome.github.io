# DingTalk 测试群统一配置设计

## 目标

为 digital-ops 里所有钉钉机器人提供统一的测试群切换能力：
- 通过一个环境变量即可把消息从正式群切换到测试群。
- 测试群配置集中管理，避免每个机器人各自硬编码。
- 对现有业务脚本侵入最小。

## 范围

- 覆盖当前所有钉钉机器人：杭州日报机器人、渠道日报机器人。
- 未来新增机器人可复用同一机制。
- 全局默认一个测试群；后续如需按业务线分群，可在配置里扩展 key。

## 非目标

- 不替换 `common/dingtalk.py` 里的发送实现。
- 不强制所有脚本改用新的发送器；只做目标群选择。

## 配置

### `common/test_groups.json`

集中存放测试群定义。真实配置文件不入仓，提交模板 `common/test_groups.example.json`：

```json
{
  "default": {
    "groupName": "功能验证群",
    "groupNumber": "197925004762",
    "openConversationId": "<测试群 openConversationId>"
  }
}
```

- `default`：所有机器人在测试模式下的默认目标群。
- `groupNumber` 仅用于人工核对，发送时仍使用 `openConversationId`。
- 未来可新增 `"daily"`、`"channel"` 等 key 实现按业务线分群。

## 模块设计

### `common/test_group.py`

提供两个公开接口：

#### `load_test_groups(path=None)`

读取 `common/test_groups.json`，返回解析后的字典。失败时抛出清晰的异常。

#### `resolve_target(robot_config, mode=None)`

参数：
- `robot_config`：当前机器人的正式群配置，通常来自 `CONFIG["robot"]` 或 `CONFIG["push"]`。
- `mode`：可选。`"test"` 强制走测试群；`None` 时读取 `TEST_MODE` 环境变量。

返回：
- 非测试模式：原样返回 `robot_config`。
- 测试模式：返回一个新字典，保留 `robotCode`，用测试群的 `openConversationId` / `groupName` 覆盖正式群信息。

环境变量：
- `TEST_MODE=1`：启用测试群。
- 其他值或空：使用正式群。

### 对 webhook 机器人的兼容

`robot_config` 可能包含 `mode: "webhook"` 和 `webhook`/`secret`。`resolve_target` 优先判断：
- 如果测试群配置里提供了 `webhook`，则返回测试群的 webhook 地址。
- 否则保留原 `robotCode`，仅覆盖 `openConversationId`。

## 使用示例

### 杭州日报机器人（企业机器人 groupSend）

```python
from common.test_group import resolve_target

robot = resolve_target(CONFIG["robot"])
client.send_group_markdown(
    robot["robotCode"],
    robot["openConversationId"],
    title, text, at_user_ids=at_ids
)
```

### 渠道日报机器人（webhook）

```python
from common.test_group import resolve_target

target = resolve_target(CONFIG["push"])
if target.get("mode") == "webhook":
    client.send_webhook_markdown(
        target["webhook"], title, text, secret=target.get("secret")
    )
else:
    client.send_group_markdown(
        target["robotCode"], target["openConversationId"], title, text
    )
```

## 改动点

1. 新增 `common/test_groups.example.json` 作为配置模板。
2. 新增 `common/test_group.py`。
3. 在 `.gitignore` 里加入 `common/test_groups.json`，避免真实 `openConversationId` 入仓。
4. 在每个发送点把 `CONFIG["robot"]` / `CONFIG["push"]` 包一层 `resolve_target`。
5. 从各机器人 `config.example.json` 里删除重复的 `verify` 测试群节点，统一走 `common/test_groups.json`。

## 测试

- 单元测试 `resolve_target`：
  - `TEST_MODE` 未设置时返回原配置。
  - `TEST_MODE=1` 时返回测试群配置。
  - 显式 `mode="test"` 时无视环境变量。
- 集成测试：开启 `TEST_MODE=1` 后运行脚本，验证消息发到「功能验证群」。

## 风险与回退

- 风险：忘记切回正式模式导致测试消息误发正式群（或反之）。
- 缓解：脚本日志里打印最终使用的 `groupName`；CI 默认不设 `TEST_MODE`。
- 回退：删除 `resolve_target` 调用即可恢复原有逻辑。
