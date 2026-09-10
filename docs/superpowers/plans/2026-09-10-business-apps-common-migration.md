# Business / Apps / Common 目录迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将日报机器人和渠道运营脚本重组为 `business → apps → common` 的单向依赖结构，并保持本地配置、测试群路由和现有功能行为不变。

**Architecture:** `business/` 保存线上/线下业务入口、业务配置与地区规则；`apps/` 保存可复用的日报机器人和钉钉播报能力；`common/` 仅保存 DingTalk、WDT 等技术客户端与通用工具。所有可执行入口改为 `python -m` 模块命令，从而移除依赖目录深度的 `sys.path.insert()` 与 `Path.parents[N]` 根目录推导。

**Tech Stack:** Python 3、标准库、`unittest`、DingTalk Open Platform / Stream、WDT OpenAPI、GitHub Actions。

---

## 范围和安全约束

- 只改主工作区 `E:\repos\digital-ops`；绝不修改 `.worktrees/public-data-integration-foundation`。
- 不读取、展示、暂存或提交 `config.json`、`wdt_credentials.json`、`common/dingtalk/test_groups.json`、`config-local/`、日志、状态和 HTML 生成物。
- 不执行任何实际钉钉写表、群推送、DING、外部 cron、Windows 计划任务或自启注册操作。
- 不修改外部 scheduler。仓库文档只提供新的模块命令，并明确由运维人员在确认后切换。
- 使用精确文件路径暂存；不得使用 `git add .` 或 `git add -A`。仅当用户明确要求时才创建本地提交，绝不自动推送。
- 历史设计/计划文档是历史记录，不批量改写其中的旧路径；仅更新面向当前运行的 README、部署指南、命令速查表与 CI。

## 最终目录结构

```text
business/
  __init__.py
  online/
    __init__.py
    channel_daily/
      __init__.py
      main.py
      run_today.py
      daily_scheduler.py
      channel_report.py
      discover_tables.py
      dingtalk_client.py
      wdt_client.py
      stock_alert.py
      purchase_alert.py
      order_risk_alert.py
      hot_items_monitor.py
      config.example.json
      requirements.txt
      README_ECS部署指南.md
      config.json                 # 本地忽略文件，不入库
      wdt_credentials.json        # 本地忽略文件，不入库
      logs/                       # 本地忽略目录
  offline/
    __init__.py
    hangzhou/
      __init__.py
      daily/
        __init__.py
        reminder.py
        listener.py
        org_sync.py
        recalc_totals.py
        check_data.py
        leaderboard_report.py
        push_leaderboard.py
        register_autostart.bat
        config.example.json
        README.md
        config.json               # 本地忽略文件，不入库
        logs/                     # 本地忽略目录
    shaoxing/
      __init__.py
      daily/
        __init__.py
        reminder.py
        listener.py
        org_sync.py
        recalc_totals.py
        check_data.py
        leaderboard_report.py
        register_autostart.bat
        config.example.json
        README.md
        config.json               # 本地忽略文件，不入库
        logs/                     # 本地忽略目录
apps/
  __init__.py
  broadcast/
    __init__.py
    dingtalk.py
  robot/
    __init__.py
    daily/
      __init__.py
      core.py
      listener.py
      leaderboard.py
common/
  __init__.py
  calendar_utils.py
  dingtalk/
    __init__.py
    client.py
    test_group.py
    test_groups.example.json
  wdt/
    __init__.py
    client.py
```

### 入口命令基线

所有命令必须在仓库根目录执行：

```bash
python -m business.offline.hangzhou.daily.reminder --status
python -m business.offline.hangzhou.daily.reminder --remind
python -m business.offline.hangzhou.daily.reminder --check
python -m business.offline.hangzhou.daily.listener
python -m business.offline.hangzhou.daily.org_sync --status
python -m business.offline.hangzhou.daily.org_sync --sync
python -m business.offline.hangzhou.daily.recalc_totals
python -m business.offline.hangzhou.daily.check_data
python -m business.offline.hangzhou.daily.leaderboard_report --send

python -m business.offline.shaoxing.daily.reminder --status
python -m business.offline.shaoxing.daily.reminder --remind
python -m business.offline.shaoxing.daily.reminder --check
python -m business.offline.shaoxing.daily.listener
python -m business.offline.shaoxing.daily.org_sync --status
python -m business.offline.shaoxing.daily.org_sync --sync
python -m business.offline.shaoxing.daily.recalc_totals
python -m business.offline.shaoxing.daily.check_data
python -m business.offline.shaoxing.daily.leaderboard_report --send

python -m business.online.channel_daily.main --dry
python -m business.online.channel_daily.main --date 2026-09-10
python -m business.online.channel_daily.run_today --dry
python -m business.online.channel_daily.run_today --date 2026-09-10
python -m business.online.channel_daily.discover_tables
python -m business.online.channel_daily.stock_alert --dry
python -m business.online.channel_daily.purchase_alert --dry --hours 8
python -m business.online.channel_daily.order_risk_alert --dry --today
python -m business.online.channel_daily.hot_items_monitor --dry
```

## 文件迁移映射

| 当前路径 | 目标路径 | 处理 |
|---|---|---|
| `common/dingtalk.py` | `common/dingtalk/client.py` | 移动；由包入口兼容导出。 |
| `common/test_group.py` | `common/dingtalk/test_group.py` | 移动；默认配置文件改为同目录。 |
| `common/test_groups.example.json` | `common/dingtalk/test_groups.example.json` | 移动模板。 |
| `common/wdt.py` | `common/wdt/client.py` | 移动；由包入口兼容导出。 |
| `common/broadcaster.py` | `apps/broadcast/dingtalk.py` | 移动为应用能力。 |
| `common/daily_robot/` | `apps/robot/daily/` | 整目录移动，保留模块职责。 |
| `数字化/钉钉/杭州日报机器人/` | `business/offline/hangzhou/daily/` | 移动业务目录；两个入口重命名为 `reminder.py`、`listener.py`。 |
| `数字化/钉钉/shaoxing_daily_robot/` | `business/offline/shaoxing/daily/` | 移动业务目录；两个入口重命名为 `reminder.py`、`listener.py`。 |
| `数字化/钉钉/渠道日报机器人/` | `business/online/channel_daily/` | 移动业务目录，脚本名保持。 |

### Task 1: 建立迁移前基线与受控文件清单

**Files:**
- Verify: `.gitignore`
- Verify: `tests/common/test_daily_robot.py`
- Verify: `tests/common/test_dingtalk.py`
- Verify: `tests/common/test_test_group.py`
- Verify: `.github/workflows/ci.yml`

- [ ] **Step 1: 记录工作区状态，不改动任何文件。**

Run:
```bash
git status --short
git diff --check
python -m unittest discover -s tests -t . -v
```

Expected: 记录所有既有改动和未跟踪文件；单元测试输出成为迁移前对照。若测试本已失败，停止迁移并先单独记录失败项，不能将既有失败归因于本次迁移。

- [ ] **Step 2: 仅列出敏感和运行时文件的 Git 忽略规则，不读取内容。**

Run:
```bash
git check-ignore -v \
  "数字化/钉钉/杭州日报机器人/config.json" \
  "数字化/钉钉/shaoxing_daily_robot/config.json" \
  "数字化/钉钉/渠道日报机器人/config.json" \
  "数字化/钉钉/渠道日报机器人/wdt_credentials.json" \
  "common/test_groups.json"
```

Expected: 每个本地配置均由 `.gitignore` 覆盖；若存在未忽略项，先完成 Task 8 的精确忽略规则更新后才允许目录移动。

- [ ] **Step 3: 确认本计划外的未跟踪文档不会被纳入迁移。**

不得暂存以下现有未跟踪文件：
```text
docs/superpowers/plans/2026-09-09-daily-robot-quality-refactor.md
docs/superpowers/plans/2026-09-09-public-data-integration-foundation.md
docs/superpowers/specs/2026-09-10-finance-raw-dingtalk-table-mapping.md
```

### Task 2: 迁移技术客户端到按技术域划分的 common 包

**Files:**
- Create: `common/dingtalk/__init__.py`
- Move: `common/dingtalk.py` → `common/dingtalk/client.py`
- Move: `common/test_group.py` → `common/dingtalk/test_group.py`
- Move: `common/test_groups.example.json` → `common/dingtalk/test_groups.example.json`
- Create: `common/wdt/__init__.py`
- Move: `common/wdt.py` → `common/wdt/client.py`
- Modify: `common/dingtalk/test_group.py`

- [ ] **Step 1: 创建包目录和包入口，不改变外部 `common.dingtalk` 与 `common.wdt` API。**

`common/dingtalk/__init__.py` 必须为：
```python
from .client import DingTalkClient, DingTalkError, send_markdown
from .test_group import load_test_groups, resolve_target

__all__ = [
    "DingTalkClient",
    "DingTalkError",
    "send_markdown",
    "load_test_groups",
    "resolve_target",
]
```

`common/wdt/__init__.py` 必须为：
```python
from .client import WdtClient, WdtError

__all__ = ["WdtClient", "WdtError"]
```

- [ ] **Step 2: 使用 `git mv` 移动已跟踪源码和模板。**

Run:
```bash
mkdir -p common/dingtalk common/wdt
git mv common/dingtalk.py common/dingtalk/client.py
git mv common/test_group.py common/dingtalk/test_group.py
git mv common/test_groups.example.json common/dingtalk/test_groups.example.json
git mv common/wdt.py common/wdt/client.py
```

Expected: `git status --short` 只显示上述重命名与新包入口；不得显示真实测试群配置或凭据。

- [ ] **Step 3: 把测试群配置默认位置改为新包同目录。**

在 `common/dingtalk/test_group.py` 中保留同名配置加载逻辑，只保留以下默认路径定义：
```python
_DEFAULT_PATH = Path(__file__).with_name("test_groups.json")
```

这样 `common/dingtalk/test_groups.json` 与 `test_groups.example.json` 同目录，且调用方仍可通过 `resolve_target()` 保持原有 `TEST_MODE=1` 语义。

- [ ] **Step 4: 运行 DingTalk 与测试群单元测试。**

Run:
```bash
python -m unittest tests.common.test_dingtalk tests.common.test_test_group -v
```

Expected: 测试通过；现有 `from common.dingtalk import ...` 导入无需改动。

### Task 3: 迁移可复用应用能力到 apps

**Files:**
- Create: `apps/__init__.py`
- Create: `apps/robot/__init__.py`
- Move: `common/daily_robot/` → `apps/robot/daily/`
- Create: `apps/broadcast/__init__.py`
- Move: `common/broadcaster.py` → `apps/broadcast/dingtalk.py`
- Modify: `apps/robot/daily/core.py`
- Modify: `apps/robot/daily/listener.py`
- Modify: `apps/robot/daily/leaderboard.py`

- [ ] **Step 1: 建立应用包入口。**

`apps/robot/daily/__init__.py` 保留原 `common/daily_robot/__init__.py` 的全部导出；不能借迁移删除任何现有公开函数。`apps/broadcast/__init__.py` 必须为：
```python
from .dingtalk import Broadcaster

__all__ = ["Broadcaster"]
```

- [ ] **Step 2: 移动共享日报和播报实现。**

Run:
```bash
mkdir -p apps/robot apps/broadcast
git mv common/daily_robot apps/robot/daily
git mv common/broadcaster.py apps/broadcast/dingtalk.py
```

- [ ] **Step 3: 删除共享应用模块内的根目录注入，并替换技术依赖。**

从 `apps/robot/daily/core.py`、`listener.py`、`leaderboard.py` 删除完整的 `Path`/`sys` 根目录推导及 `sys.path.insert(...)` 代码块。`core.py` 的导入必须改为：
```python
from common.dingtalk import DingTalkClient, DingTalkError, send_markdown
from common.dingtalk.test_group import resolve_target
```

`listener.py` 和 `leaderboard.py` 保持：
```python
from common.dingtalk import DingTalkClient
```

除上述导入和根目录注入外，不改变日报计算、DING 文本或群消息行为。

- [ ] **Step 4: 验证应用包能够在仓库根目录导入。**

Run:
```bash
python -c "from apps.robot.daily import today_info, ReportHandler; from apps.broadcast import Broadcaster; print('imports ok')"
```

Expected: 输出 `imports ok`，没有 `ModuleNotFoundError`。

### Task 4: 迁移杭州日报业务入口与运行资产

**Files:**
- Move: `数字化/钉钉/杭州日报机器人/` → `business/offline/hangzhou/daily/`
- Rename: `hangzhou_reminder.py` → `reminder.py`
- Rename: `hangzhou_listener.py` → `listener.py`
- Create: `business/__init__.py`, `business/offline/__init__.py`, `business/offline/hangzhou/__init__.py`, `business/offline/hangzhou/daily/__init__.py`
- Modify: `business/offline/hangzhou/daily/{reminder,listener,org_sync,recalc_totals,check_data,leaderboard_report,push_leaderboard}.py`
- Modify: `business/offline/hangzhou/daily/register_autostart.bat`

- [ ] **Step 1: 移动业务目录并创建 Python 包标识。**

Run:
```bash
mkdir -p business/offline/hangzhou
git mv "数字化/钉钉/杭州日报机器人" business/offline/hangzhou/daily
git mv business/offline/hangzhou/daily/hangzhou_reminder.py business/offline/hangzhou/daily/reminder.py
git mv business/offline/hangzhou/daily/hangzhou_listener.py business/offline/hangzhou/daily/listener.py
```

为 `business/`、`business/offline/`、`business/offline/hangzhou/`、`business/offline/hangzhou/daily/` 分别创建空的 `__init__.py`。保留目录内 `config.json`、`.reminder_state.json`、`org_input_*.json`、`logs/`、`leaderboard.html` 的相对 `BASE_DIR` 行为，但绝不将它们暂存。

- [ ] **Step 2: 将共享日报导入替换为应用层包导入。**

在下列文件中替换：
```python
from common.daily_robot import ...
```
为：
```python
from apps.robot.daily import ...
```

适用文件：`reminder.py`、`listener.py`、`org_sync.py`、`recalc_totals.py`、`check_data.py`、`leaderboard_report.py`。

在 `reminder.py` 中，把：
```python
from common.test_group import resolve_target
```
替换为：
```python
from common.dingtalk.test_group import resolve_target
```

保留：
```python
from common.dingtalk import DingTalkClient
```

- [ ] **Step 3: 用包内相对导入替换杭州本地脚本导入。**

在 `org_sync.py` 中删除 `sys.path.insert(0, str(BASE_DIR))` 与：
```python
from hangzhou_reminder import send_group
```

替换为：
```python
from .reminder import send_group
```

在 `push_leaderboard.py` 中删除为本地导入添加的 `sys.path.insert(0, str(BASE_DIR))`。将：
```python
from leaderboard_report import (...)
```
替换为：
```python
from .leaderboard_report import (...)
```

此步骤只修正模块路径；若导入的 `CAL`、`MONTH`、`REST`、`WORKDAYS`、`TARGET_COL`、`DEPT_ORDER`、`DEPT_LABEL`、`BC_EXCLUDE`、`parse_num` 仍未由 `leaderboard_report.py` 导出，记录为既有接口不一致，停止并在 Task 9 中隔离验证，不能在目录迁移中猜测业务算法。

- [ ] **Step 4: 删除杭州各脚本的 repo-root 路径注入。**

从 `reminder.py`、`listener.py`、`org_sync.py`、`recalc_totals.py`、`check_data.py`、`leaderboard_report.py`、`push_leaderboard.py` 删除完整的：
```python
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
```

如果删除后 `sys`、`Path` 不再被其余业务逻辑使用，一并删除对应未使用导入；`BASE_DIR = Path(__file__).parent` 仍用于相对配置、状态和日志文件，必须保留。

- [ ] **Step 5: 更新监听自启脚本，使它从仓库根目录运行模块入口。**

保留现有启动/停止包装逻辑，但 Python 启动行必须等价于：
```bat
cd /d "%~dp0\..\..\..\.."
"%USERPROFILE%\miniconda3\python.exe" -m business.offline.hangzhou.daily.listener
```

不得在脚本中硬编码用户机器绝对仓库路径；在实际替换前先读取现有 BAT 文件，保留其已有的后台启动、PID 或 VBS 行为。

### Task 5: 迁移绍兴日报业务入口与运行资产

**Files:**
- Move: `数字化/钉钉/shaoxing_daily_robot/` → `business/offline/shaoxing/daily/`
- Rename: `shaoxing_reminder.py` → `reminder.py`
- Rename: `shaoxing_listener.py` → `listener.py`
- Create: `business/offline/shaoxing/__init__.py`, `business/offline/shaoxing/daily/__init__.py`
- Modify: `business/offline/shaoxing/daily/{reminder,listener,org_sync,recalc_totals,check_data,leaderboard_report}.py`
- Modify: `business/offline/shaoxing/daily/register_autostart.bat`

- [ ] **Step 1: 移动绍兴业务包。**

Run:
```bash
mkdir -p business/offline/shaoxing
git mv "数字化/钉钉/shaoxing_daily_robot" business/offline/shaoxing/daily
git mv business/offline/shaoxing/daily/shaoxing_reminder.py business/offline/shaoxing/daily/reminder.py
git mv business/offline/shaoxing/daily/shaoxing_listener.py business/offline/shaoxing/daily/listener.py
```

为 `business/offline/shaoxing/` 和 `business/offline/shaoxing/daily/` 创建空 `__init__.py`。本地配置、状态、组织输入和日志文件随目录保留，但不得暂存。

- [ ] **Step 2: 替换共享日报导入和路径注入。**

在 `reminder.py`、`listener.py`、`org_sync.py`、`recalc_totals.py`、`check_data.py`、`leaderboard_report.py` 中将：
```python
from common.daily_robot import ...
```
替换为：
```python
from apps.robot.daily import ...
```

删除每个文件中完整的 repo-root `Path.parents[2]`/`sys.path.insert(...)` 块。保留 `BASE_DIR` 及其对于本地配置、日志和状态文件的使用。

- [ ] **Step 3: 将监听器自启命令改为模块入口。**

在保留原有 Windows 后台包装逻辑的前提下，Python 启动行必须等价于：
```bat
cd /d "%~dp0\..\..\..\.."
"%USERPROFILE%\miniconda3\python.exe" -m business.offline.shaoxing.daily.listener
```

### Task 6: 迁移线上渠道日报业务入口与兼容包装

**Files:**
- Move: `数字化/钉钉/渠道日报机器人/` → `business/online/channel_daily/`
- Create: `business/online/__init__.py`, `business/online/channel_daily/__init__.py`
- Modify: `business/online/channel_daily/{main,run_today,stock_alert,purchase_alert,order_risk_alert,hot_items_monitor,discover_tables,daily_scheduler,dingtalk_client,wdt_client}.py`

- [ ] **Step 1: 移动渠道业务包。**

Run:
```bash
mkdir -p business/online
git mv "数字化/钉钉/渠道日报机器人" business/online/channel_daily
```

创建 `business/online/__init__.py` 与 `business/online/channel_daily/__init__.py`。保留本地 `config.json`、`wdt_credentials.json`、`logs/`、缓存和状态 JSON、`hot-items-page/` 的相对位置，不读取或暂存真实内容。

- [ ] **Step 2: 使同目录模块使用包内导入。**

`main.py` 中必须使用：
```python
from .dingtalk_client import DingTalkClient, DingTalkError
from . import channel_report as cr
from common.dingtalk.test_group import resolve_target
```

`discover_tables.py` 使用：
```python
from .dingtalk_client import DingTalkClient, DingTalkError
```

`hot_items_monitor.py`、`order_risk_alert.py`、`purchase_alert.py`、`stock_alert.py` 中将：
```python
from wdt_client import WdtClient
```
替换为：
```python
from .wdt_client import WdtClient
```

- [ ] **Step 3: 保留业务目录中的技术客户端兼容薄包装。**

`dingtalk_client.py` 只保留对公共客户端的兼容导出：
```python
from common.dingtalk import DingTalkClient, DingTalkError

__all__ = ["DingTalkClient", "DingTalkError"]
```

`wdt_client.py` 只保留：
```python
from common.wdt import WdtClient, WdtError

__all__ = ["WdtClient", "WdtError"]
```

不要把 `main.py`、发现工具或报警脚本直接耦合到公共客户端内部路径；兼容模块保持业务包的既有局部接口。

- [ ] **Step 4: 删除渠道脚本的根目录注入并更新测试群导入。**

从 `dingtalk_client.py`、`wdt_client.py`、`run_today.py`、`stock_alert.py`、`purchase_alert.py`、`order_risk_alert.py`、`hot_items_monitor.py` 中删除所有 `Path(...).parents[...]` 与 `sys.path.insert(...)` 根目录注入。

把下列脚本中的：
```python
from common.test_group import resolve_target
```
替换为：
```python
from common.dingtalk.test_group import resolve_target
```

适用文件：`main.py`、`run_today.py`、`stock_alert.py`、`purchase_alert.py`、`order_risk_alert.py`、`hot_items_monitor.py`。

保留这些脚本的：
```python
from common.dingtalk import DingTalkClient, send_markdown
```

### Task 7: 更新测试以验证新依赖边界

**Files:**
- Modify: `tests/common/test_daily_robot.py`
- Modify: `tests/common/test_test_group.py`
- Modify: `tests/common/test_dingtalk.py`

- [ ] **Step 1: 将日报机器人测试改为应用层导入和 mock 路径。**

把：
```python
from common.daily_robot.core import (
```
替换为：
```python
from apps.robot.daily.core import (
```

将全部：
```python
@patch("common.daily_robot.core.send_group")
@patch("common.daily_robot.core.fetch_status")
```
替换为：
```python
@patch("apps.robot.daily.core.send_group")
@patch("apps.robot.daily.core.fetch_status")
```

- [ ] **Step 2: 将测试群测试改为新技术域包位置。**

把：
```python
from common import test_group
```
替换为：
```python
from common.dingtalk import test_group
```

保留现有用例对 `load_test_groups` 的替换方式和 `TEST_MODE` 语义，不能将测试改成读取真实配置。

- [ ] **Step 3: 删除只为导入仓库根目录存在的测试路径注入。**

从三个测试文件删除：
```python
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
```

因为测试命令始终使用 `python -m unittest discover -s tests -t . -v`，仓库根目录已由 `-t .` 作为顶层模块路径。

- [ ] **Step 4: 先运行受影响测试。**

Run:
```bash
python -m unittest tests.common.test_daily_robot tests.common.test_dingtalk tests.common.test_test_group -v
```

Expected: 受影响的单元测试全部通过，且不发生任何网络/API 调用。

### Task 8: 更新忽略规则、CI 与当前运行文档

**Files:**
- Modify: `.gitignore`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `docs/命令速查表.md`
- Modify: `business/offline/hangzhou/daily/README.md`
- Modify: `business/offline/shaoxing/daily/README.md`
- Modify: `business/online/channel_daily/README_ECS部署指南.md`
- Modify: `数字化/钉钉/榜单服务/部署指南.md`
- Modify: `docs/云迁移指南.md`

- [ ] **Step 1: 精确迁移 `.gitignore` 路径。**

将旧路径替换为以下目标路径：
```gitignore
business/offline/hangzhou/daily/leaderboard.html
business/online/channel_daily/hot-items-page/*
!business/online/channel_daily/hot-items-page/.gitkeep
common/dingtalk/test_groups.json
```

删除仅针对不存在旧业务目录的规则。保留 `数字化/钉钉/榜单页面/` 与 `数字化/钉钉/榜单服务/data/` 的忽略规则，除非它们在本次迁移中确实移动；本计划不移动榜单服务或榜单页面目录。

- [ ] **Step 2: 将 CI 编译范围改为新三层目录。**

在 `.github/workflows/ci.yml` 中将：
```yaml
python -m compileall -q "数字化/钉钉/杭州日报机器人" "数字化/钉钉/渠道日报机器人"
```
替换为：
```yaml
python -m compileall -q common apps business
```

保留现有 `config.example.json` 的 JSON 校验，不向 CI 注入任何真实凭据或钉钉调用。

- [ ] **Step 3: 更新根 README 的结构与入口说明。**

用下列树替换旧的机器人目录描述：
```text
business/
  online/channel_daily/          线上渠道日报与运营提醒业务入口
  offline/hangzhou/daily/        杭州日报业务入口和地区配置
  offline/shaoxing/daily/        绍兴日报业务入口和地区配置
apps/
  robot/daily/                   可复用日报填报、提醒、组织同步和榜单能力
  broadcast/                     可复用钉钉播报能力
common/
  dingtalk/                      钉钉技术客户端与测试群路由
  wdt/                           旺店通技术客户端
```

紧随其后明确写出：所有脚本从仓库根目录用 `python -m business...` 运行；目录迁移不自动修改任何 Windows 计划任务、cron 或常驻监听服务；敏感配置只保存在本地、不得提交。

- [ ] **Step 4: 更新命令速查表中的所有旧直接脚本命令。**

把 `docs/命令速查表.md` 中所有：
```bash
python 数字化/钉钉/...
```
改为本计划“入口命令基线”中的同等 `python -m business...` 命令；测试群命令保留：
```bash
PYTHONIOENCODING=utf-8 TEST_MODE=1
```

把：
```bash
python -m compileall common 数字化
```
替换为：
```bash
python -m compileall common apps business
```

把测试群配置路径改为：
```text
common/dingtalk/test_groups.json
```

- [ ] **Step 5: 更新杭州、绍兴 README 的手动命令与自启说明。**

两份 README 都使用新的 `python -m business.offline.<region>.daily...` 命令，并在自启一节说明 BAT 从仓库根目录模块化启动监听器。保留现有业务机制、成员口径和安全提醒；不改动运行配置内容。

- [ ] **Step 6: 更新渠道 ECS 部署指南，移除凭据复制表述。**

将渠道部署命令改为在仓库根目录模块执行，例如：
```bash
cd /opt/digital-ops
/usr/bin/python3 -m business.online.channel_daily.run_today --dry
```

将定时命令示例改为：
```cron
0 9 * * * cd /opt/digital-ops && /usr/bin/python3 -m business.online.channel_daily.run_today >> business/online/channel_daily/logs/cron.log 2>&1
```

部署文档必须写明 `config.json`、`wdt_credentials.json` 和 webhook 由目标环境安全注入或本地受控文件提供；不得包含“已含全部凭据、拷贝即用”或等价语义。

- [ ] **Step 7: 更新榜单服务和云迁移指南里的当前运行路径。**

将 `数字化/钉钉/榜单服务/部署指南.md` 中调用杭州 `push_leaderboard.py` 的命令改为从仓库根目录执行：
```bat
"%USERPROFILE%\miniconda3\python.exe" -m business.offline.hangzhou.daily.push_leaderboard
```

在 `docs/云迁移指南.md` 中将渠道部署指南引用改为：
```text
business/online/channel_daily/README_ECS部署指南.md
```

### Task 9: 隔离并处理杭州 push_leaderboard 既有接口不一致

**Files:**
- Verify: `business/offline/hangzhou/daily/push_leaderboard.py`
- Verify: `business/offline/hangzhou/daily/leaderboard_report.py`
- Modify only if evidence requires: the minimal one of these two files

- [ ] **Step 1: 在不发送消息的前提下验证模块导入。**

Run:
```bash
python -c "import business.offline.hangzhou.daily.leaderboard_report"
python -c "import business.offline.hangzhou.daily.push_leaderboard"
```

Expected: 第一条只验证报告模块导入；第二条可能暴露当前已知的常量/函数导出不一致。不得运行会发送群消息的业务参数。

- [ ] **Step 2: 若第二条失败，先记录完整 ImportError。**

只比较 `push_leaderboard.py` 请求的符号与 `leaderboard_report.py` 的实际公开符号，确认这些常量是否应由业务配置提供、应从 `apps.robot.daily.leaderboard` 导入，还是脚本已过时。

- [ ] **Step 3: 仅做有证据的最小修复。**

允许的修复仅是：更正已移动模块路径，或把确实存在的符号从其实际定义模块导入。禁止在迁移期间重新设计榜单指标、补造常量、改变播报内容或删除脚本。若无可证实的最小修复，应将该脚本标记为迁移前既有缺陷并从本次验收范围隔离。

### Task 10: 完整验证和变更审计

**Files:**
- Verify: `common/`, `apps/`, `business/`, `tests/`, `.github/workflows/ci.yml`, `.gitignore`, 当前运行文档

- [ ] **Step 1: 搜索旧导入与根路径注入。**

Run:
```bash
rg -n "common\.daily_robot|common\.test_group|from dingtalk_client|from wdt_client|import channel_report|from hangzhou_reminder|from leaderboard_report" common apps business tests
rg -n "sys\.path\.insert|Path\(__file__\).*parents|BASE_DIR\.parents" common apps business tests
```

Expected: 第一条不返回生产或测试代码中的旧导入；第二条不返回为仓库根目录服务的注入。允许 `BASE_DIR` 用于业务目录内配置、状态、日志的相对路径，但不得再用 `parents[N]` 猜测仓库根。

- [ ] **Step 2: 语法编译。**

Run:
```bash
python -m compileall -q common apps business
```

Expected: 退出码为 `0`，没有语法错误。

- [ ] **Step 3: 运行完整单元测试。**

Run:
```bash
python -m unittest discover -s tests -t . -v
```

Expected: 全部测试通过；不接受只运行受影响测试代替完整测试。

- [ ] **Step 4: 对不会触发外部写入的入口进行帮助/导入检查。**

Run:
```bash
python -m business.offline.hangzhou.daily.reminder --help
python -m business.offline.shaoxing.daily.reminder --help
python -m business.online.channel_daily.main --help
python -m business.online.channel_daily.run_today --help
```

Expected: 所有命令正常打印参数帮助或安全的参数说明；若某入口没有 `--help` 支持，则只执行 `python -c "import ..."`，不能为了验证而运行默认业务逻辑。

- [ ] **Step 5: 审计待暂存文件。**

Run:
```bash
git status --short
git diff --check
git diff -- common apps business tests README.md docs .github/workflows/ci.yml .gitignore
```

Expected: 不出现 `config.json`、`wdt_credentials.json`、测试群真实配置、日志、状态 JSON、缓存或 HTML 生成物；变更仅涵盖计划列出的源码、模板、测试、CI 与当前文档。

- [ ] **Step 6: 仅在用户明确要求后，按文件清单精确暂存和提交。**

必须先向用户展示拟暂存的精确路径列表。禁止广泛暂存，禁止自动提交，禁止推送。外部 cron、计划任务和常驻监听服务的切换必须由用户另行明确确认并在其目标环境操作。

## 验收标准

1. `business → apps → common` 为唯一的运行时依赖方向；`apps` 不包含杭州、绍兴或渠道业务规则。
2. 所有业务入口均能从仓库根目录通过 `python -m business...` 导入或显示帮助，不依赖目录深度和 `sys.path.insert()`。
3. DingTalk、WDT 与测试群路由仍保持现有公共 API；`TEST_MODE=1` 行为不变。
4. 业务目录继续在各自目录查找本地配置、状态、日志和运行数据。
5. 当前 README、部署说明、命令速查表、榜单服务文档、云迁移指南与 CI 均指向新目录和新模块命令。
6. 完整单元测试和 `python -m compileall -q common apps business` 均通过。
7. 无敏感配置、运行状态、日志或生成物被暂存；无外部 scheduler、DingTalk 或 RDS 状态被修改。
