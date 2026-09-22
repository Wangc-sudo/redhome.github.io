# IPv6 出网卡死：代码门禁与修复方案

2026-09-22 ｜ 状态：**已拍板并实施**（A+B 一起执行，全量 1356 例绿，待随 B3/B4 收口提交）｜ 触发：T7 冒烟 sync-dingtalk 卡死断点（当晚已闭环）

## 1. 问题与实测证据

**现象**（2026-09-22 实测，dops-app）：
- `sync-dingtalk` 进程（PID 29748/29749）卡在 IPv6 TCP connect，空转 2.5 小时；
  `sync-dingtalk-2.log` 0 字节——连第一条数据都没读出来。
- 断点提示词定位：进程阻塞于 `2401:b180:2000:70::e:443` 的 `EINPROGRESS`，
  每个钉钉 API 请求先等 IPv6 超时（~120s）再回落 IPv4。

**根因链**：ECS 带 IPv6 地址 → glibc `getaddrinfo` 默认 AAAA 优先 →
VPC 无公网 IPv6 出网 → connect 永不完成。

**影响面（全仓库 HTTP 出口审计）**：四个出口全部走 `urllib.request.urlopen`，
底层统一经 `socket.getaddrinfo`，同坑同治，一处过滤全覆盖：

| 出口 | 位置 |
|---|---|
| WDT 网关 | `common/wdt/client.py:85` |
| 钉钉 API | `common/dingtalk/client.py:42` |
| Nacos REST 客户端 | `common/public_data/nacos_client.py:31,42` |
| Pipeline 种子回读 | `common/public_data/pipeline_config.py:260` |

**裁决（2026-09-22）**：不采用运维侧 `/etc/gai.conf` 修复
（机器配置与代码分离，换机/扩机即复发，且无法进代码库评审），
改为**代码层门禁 + 代码层修复**。

## 2. 方案 A：出网门禁（fail-fast）

目标：把「空转 2.5 小时」变成「启动 5 秒内拒绝，并给出明确修复指引」。

新增 `common/public_data/ipv4_egress.py`：

```python
def ipv6_egress_stalled(host, port=443, *, getaddrinfo=socket.getaddrinfo,
                        probe=None, timeout=3.0):
    """True 当且仅当：DNS 返回 AAAA 优先 且 IPv6 出网不通。

    两步判定（短路）：
      1. getaddrinfo(host, port) 首个结果族 != AF_INET6 → False（IPv4 优先，无风险）
      2. AAAA 优先 → 对该 IPv6 地址短超时 connect 探测：
         通 → False（有 IPv6 出网）；不通 → True（卡死场景）
    """
```

接入点：`live_safety.py` 的 `require_live_run` 追加一步
`require_ipv4_egress(...)`（风格与现有 target 门禁一致，`environ` 可注入、
socket/probe 可注入，测试全 mock、零外网依赖）。

行为（与方案 B 联动，见 §4）：
- B 已启用（默认）：AAAA 已被过滤、无卡死路径，门禁不探测、直接放行（零探测开销）；
- B 被显式关闭（`PUBLIC_DATA_ALLOW_IPV6=1`）：门禁真探测，
  卡死场景 `raise LiveRunRejected("ipv6_egress_stall: ...; unset PUBLIC_DATA_ALLOW_IPV6 to force IPv4, or fix IPv6 egress")`。

## 3. 方案 B：代码强制 IPv4（根治）

同一模块提供：

```python
def force_ipv4():
    """进程级：socket.getaddrinfo 过滤掉 AF_INET6 结果（幂等）。"""
```

- 挂载点：`cli.py` 的 `_handle_live_sync`（及 gateway/scheduler 启动路径）
  **最前面**——先于任何 DNS 解析与 HTTP 调用。
- 默认启用；`PUBLIC_DATA_ALLOW_IPV6=1` 可关闭（未来 VPC 开通 IPv6 出网后平滑切换）。
- 一处 patch 覆盖 §1 全部四个 urllib 出口；仅本进程生效，不改系统配置。
- 风险面评估：RDS/Redis 走内网、解析结果本就是 IPv4，不受影响；
  测试套件使用假 transport、不经真 DNS，不受影响。

**启动顺序（关键）**：先跑门禁探测（读原始 `getaddrinfo` 的真实环境），
再 `force_ipv4()`。顺序反了门禁永远探测不到 AAAA。

## 4. 备选方案对比

| 方案 | 层级 | 效果 | 代价/风险 | 结论 |
|---|---|---|---|---|
| **A 出网门禁** | 代码 | fail-fast，永不静默空转 | 只检测不修复，需配 B | **必做** |
| **B 强制 IPv4** | 代码 | 根治；env 开关可逆 | getaddrinfo 进程级 patch（需测试钉住） | **推荐** |
| C gai.conf | 运维 | 根治，不改代码 | 三台都要配；换机/重装即复发；无法进库评审 | 降级为应急过渡 |
| D VPC 开 IPv6 出网 | 云侧 | 根治 | 依赖运营商与控制台，周期长 | 远期再评估 |
| E 缩短 connect 超时 | 代码 | 缓解（120s→10s/请求） | 不根治，28 表×分页仍慢 | 不做 |

## 5. 实施清单

| # | 改动 | 文件 | 预估 |
|---|---|---|---|
| 1 | `ipv6_egress_stalled()` + `force_ipv4()` | 新建 `common/public_data/ipv4_egress.py` | ~60 行 |
| 2 | `require_ipv4_egress()` 门禁接入 | `common/public_data/live_safety.py` | ~30 行 |
| 3 | cli/gateway 启动挂载（探测→patch 顺序） | `common/public_data/cli.py` | ~15 行 |
| 4 | 测试：AAAA 优先×{通,不通}、IPv4 优先、B 开/关、patch 生效断言 | `tests/common/test_ipv4_egress.py` + `test_live_safety.py` | ~120 行 |
| 5 | 断点提示词 §A 第一步改写；gai.conf 降级为过渡 | `docs/上云推进续接提示词-2026-09-22.md` | ~20 行 |

预估半天内完成（含 1320 例全量回归）。改代码走既有铁律：
本地改 → 全量测试绿 → commit（只 add 相关文件，避开 WDT 脏文件）→ scp 到 `/opt/dops/repo`。

## 6. 验收

1. 干净机器（未改 gai.conf）上 `live-sync` 直接走 IPv4，无 120s 卡死。
2. `PUBLIC_DATA_ALLOW_IPV6=1` 且环境「AAAA 优先 + 无 IPv6 出网」：
   启动 5s 内 `LiveRunRejected`，信息含 `ipv6_egress_stall` 与修复指引。
3. 全量测试 1320+ 绿，测试零外网依赖。
4. CLI stdout 契约不变（仍只有安全摘要行）。

## 7. 与既有产物的关系

- **断点提示词**：§A 第一步由「三台改 gai.conf」改为「部署含本方案的新代码」；
  gai.conf 命令保留为应急过渡附录。
- **failure_code**：沿用断点提示词中已使用的 `ipv6_egress_stall`
  （卡死 run 的标记值与门禁错误码同名，语义统一）。
- **observability spec**（2026-09-22-live-sync-observability.md）：天然叠加——
  门禁拒绝经 `LiveRunRejected` 路径产生明确错误，per-dataset 日志不受影响。
- **卡死现场清理**（独立于本方案，仍需执行）：pkill 29748/29749；
  `mart_ops.sync_runs` 中 `status='started'` 的卡死 run UPDATE 为
  `status='failed', failure_code='ipv6_egress_stall'`（注意主键列名是
  `sync_run_id`，不是 `run_id`）。

## 8. 决策记录（2026-09-22 已拍板）

- **D1** B **默认开**（"代码中不可以使用 IPv6"为默认行为），`PUBLIC_DATA_ALLOW_IPV6=1` 显式关闭。
- **D2** 作用域：**live-sync + gateway 双入口挂载**（`_handle_live_sync` / `_handle_run`，
  门禁探测先于 `force_ipv4`）；scheduler 经 subprocess 调 cli，天然继承。
- **D3** gai.conf：实际作为**应急过渡**已于 09-22 晚在 dops-app 上机并跑通 T7；
  代码合并后定位为「系统层可选双保险」（见 `docs/云主机初始化基线.md` §1 第 3 级），
  可留可撤，不构成双轨（代码基线不依赖它）。
- **D4** 时序：T7 当晚以 gai.conf 应急闭环；门禁代码转为**长期防护**，
  随 B3/B4 收口提交部署；observability（task 1）仍排其后。
