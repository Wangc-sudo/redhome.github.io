# live_sync 可观测性增强建设方案

2026-09-22 ｜ 状态：草案（源自 09-21 WDT 30 天回补实战复盘）

## 1. 背景与问题

2026-09-21 执行 WDT 30 天回补（4320 个窗口），同一故障连续杀死 3 次同步运行，
每次报错只有一行安全摘要：

```
run_id=unknown status=failed code=sync_error
```

CLI 的非泄漏设计（不打印堆栈/载荷/URL）本身是对的，但它把**所有**源侧
故障压扁成 `source_read_failed`，运维侧无法区分：

| 真实根因（本次全部遇到） | 被压扁成的码 |
|---|---|
| 网关瞬时错误（重放即成功） | source_read_failed |
| 翻页漂移撞重复 ID 守卫 | source_read_failed |
| 行键配置错误（purchase_no 撞键） | source_read_failed |

最终只能靠临时挂载一个 verbose 探针脚本逐窗口打印才定位。
**安全沉默 ≠ 可运维**——缺的是官方的、不泄漏的运行时进度通道。

## 2. 目标

1. 任何一次同步失败，事后仅凭容器日志 + DB 即可定位到**具体数据集与根因类别**，
   无需临时探针。
2. 不破坏 CLI 非泄漏约定：stdout 仍只有安全摘要行；详细信息走
   logging（容器日志）与 DB 控制面。
3. 长时间运行（回补数千窗口）可外部观测进度（完成 N/总数）。

## 3. 方案

### 3.1 per-dataset 进度日志（live_sync）

在 `LiveSyncService` 的 DingTalk/WDT 数据集循环中，每个数据集处理前后
各打一条 `logging.INFO`：

```
INFO dataset=wdt_purchase_orders_0350 window=2026-09-03T10:16:14Z..2026-09-03T11:06:14Z start
INFO dataset=wdt_purchase_orders_0350 records=183 written=183 duration=4.2s
```

失败时打 `logging.ERROR`（含异常类名与摘要，**不含凭据/URL/堆栈**——
异常消息在本代码库中只携带数据集名、记录 ID、网关状态码，见
`wdt_read.py` / `wdt/client.py` 的消息构造）：

```
ERROR dataset=wdt_purchase_orders_0350 error=WdtReadError: duplicate record id 'CG202608310004' within one page ...
```

容器内 logging 默认落 stderr → `docker logs` 可见；CLI stdout 契约不变，
`cli.py` 顶部注释的非泄漏范围明确为「stdout 摘要行」，日志通道另行约定。

### 3.2 failure_code 细化（sync_runs）

`_failure_code()` 目前是 4 类粗码。扩展为「粗码:异常类名」，
仍受 `varchar(100)` 限制（类名截断至 40 字符）：

```
source_read_failed:PaginationDriftExceeded
source_read_failed:WdtError
schema_drift:DingTalkReadError
```

DB 侧无需改表结构；旧码保持前缀兼容（已有按粗码过滤的查询不受影响）。

### 3.3 进度计数（可选，阶段 2）

`sync_runs` 无进度字段。阶段 2 可在 `sync_dataset_summary` 落库即天然
提供「已完成数据集数」（回补实测中 summary 是逐数据集写入的，可直接
`COUNT(*) WHERE sync_run_id=?` 当进度条）。仅需在运行手册中固化这条
查询，**不改代码**。

## 4. 非目标

- 不改 CLI stdout 摘要格式（下游可能有解析方）。
- 不打印堆栈/凭据/URL/载荷——与现有非泄漏约定一致。
- 不引入外部 APM/日志收集（当前规模 docker logs 足够）。

## 5. 实施清单

| # | 改动 | 文件 |
|---|---|---|
| 1 | 数据集循环加 INFO/ERROR 日志 | `common/public_data/live_sync.py` |
| 2 | `_failure_code` 追加异常类名 | `common/public_data/live_sync.py` |
| 3 | 单测：日志调用与 failure_code 新格式 | `tests/common/test_public_data_live_sync.py` |
| 4 | 运行手册补「失败定位三步」与进度查询 SQL | `docs/阶段4切换运行手册.md` |

预估：代码 ~40 行 + 测试 ~60 行，半天内完成。

## 6. 验收

1. 人为制造一个失败数据集（非法 record_id_path），重跑同步：
   `docker logs` 能看到具体数据集与根因，`sync_runs.failure_code`
   带异常类名，CLI stdout 仍只有一行安全摘要。
2. 全量测试套件通过（1320+，当前基线 1320 例绿）。
3. 回补场景（>4000 数据集）日志量可接受（每数据集 ≤2 行 INFO）。

## 7. 附：本次回补已沉淀的相关修复

- `wdt_read.py`：跨页漂移去重（同页重复仍报错），常量 `_DUPLICATE_DRIFT_TOLERANCE_PAGES`。
- `scripts/wdt_datasets.json`：采购入库行键 `purchase_no` → `order_no`
  （一张采购单多次入库，行粒度是入库单 RK…）。
- `common/public_data/scheduler.py`：常驻调度容器（Nacos 注册表 cron 驱动，
  MySQL 命名锁防重入），compose `scheduler` profile。
