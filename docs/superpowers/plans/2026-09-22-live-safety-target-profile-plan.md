# live_safety 方案 B（target profile）改造记录（2026-09-22）

> 依据：`docs/上云部署规划.md` §5.1 方案 B、`docs/上云更新方案-2026-09-21.md` §3 第 4 步。
> 性质：上云唯一代码阻塞项；本文件为完成记录。

## T0 基线

- 改造前全量：`python -m unittest discover -s tests -t .` → **1294 例 OK（skipped=39）**
- 改造后全量：**1310 例 OK（skipped=39）**（1294 + 新增 16），零回归

## 改动清单

| 文件 | 改动 |
|---|---|
| `common/public_data/deploy_targets.py` | **新增**。共享 profile 表：`local-dispose` / `cloud-managed` 两档，含 app_env、库名后缀、runner 要求、精确库名、host 判据；`PUBLIC_DATA_TARGET` 未设时按 `APP_ENV` 推导（零配置即旧行为），显式设置必须与 `APP_ENV` 一致（不一致 loud failure，`UnknownDeployTarget`） |
| `common/public_data/live_safety.py` | 重写分派层 `_require_target`：local-dispose 判据**逐字保留**（runner=1 / host=mysql / 三个 `_test` 库名）；cloud-managed 判据 = 禁 runner 标记 + 禁本地丢弃 host（mysql/localhost/127.0.0.1/::1）+ 精确正式库名（`raw_dingtalk`/`raw_wdt`/`mart_ops`）。四个门禁旗标（`--live-read`/`--live-send`/`--confirm-local-test-write`）全部保留不变 |
| `common/public_data/settings.py` | 库名后缀铁律改为查 `deploy_targets` 同一张表（不再自写 endswith 判据）；显式 TARGET 与 APP_ENV 一致性在此层即校验 |
| `tests/common/test_live_safety.py` | **新增 16 例**：local-dispose 行为钉死（5 例）+ cloud-managed 放行/拒绝（9 例）+ settings 一致性（4 例） |

## 关键设计决策（与规划的偏差登记）

| # | 决策 | 理由 |
|---|---|---|
| 1 | cloud-managed 要求**显式** `PUBLIC_DATA_TARGET=cloud-managed`，`APP_ENV=production` 单独推导出 target 但门禁拒绝放行 | 生产写路径必须显式 opt-in；防止只改 APP_ENV 就获得写权限 |
| 2 | settings.py 不强制精确库名，只守后缀铁律 | 既有 settings 测试使用任意库名（`dingtalk_test` 等）；精确库名校验收敛在门禁层，职责更清晰 |
| 3 | `UnknownDeployTarget` 继承 `ValueError` 而非 `LiveRunRejected` | 配置错误（settings 层）与运行拒绝（门禁层）分流；调用方既有 `ValueError` 捕获路径兼容 |
| 4 | cloud-managed 的 host 判据用「禁本地丢弃 host」黑名单而非白名单 | 白名单会把云厂商内网地址写死进代码；真正的牙齿在「精确库名 + 显式 opt-in + 禁 runner」 |

## 门禁矩阵（改造后）

| APP_ENV | PUBLIC_DATA_TARGET | 库名 | host | 结果 |
|---|---|---|---|---|
| test | （未设） | `*_test` 三正式名 | mysql + runner=1 | ✅ local-dispose（原行为） |
| test | cloud-managed | — | — | ❌ TARGET/APP_ENV 不一致 |
| production | （未设） | 任意 | 任意 | ❌ 生产写需显式 opt-in |
| production | cloud-managed | `raw_dingtalk`/`raw_wdt`/`mart_ops` | 非本地丢弃 host | ✅ cloud-managed |
| production | cloud-managed | 带 `_test` 或 host=localhost 等 | — | ❌ 判据拒绝 |

## 下一步（解锁）

方案 §3 第 4 步完成 → 第 5 步：dops-ctrl 起 Nacos（接 RDS 13049 + 鉴权）→ 发布三类 seed → 第 6 步 ECS-App compose 按 §2.5 注入 → 第 7 步全链路冒烟。
