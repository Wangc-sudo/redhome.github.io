# 三条逻辑线 Docker 拆分、Nacos 注册中心与控制面设计

状态：待确认（方案评审稿，确认后按阶段实施）

## 1. 背景与目标

后端代码集中在 `common/public_data`，单一 `sync-runner` 同时承担钉钉、WDT、mart 三类工作，业务机器人在宿主机上以 cron 分散运行。本设计按「三条逻辑线」拆分 Docker 容器，并以 **Nacos 作为核心骨干**——既作注册中心（服务发现）又作配置中心（业务线关停/扩线 + 服务配置），控制面只做业务化门面。

> 定调：直接采用 Nacos（重量级），跳过轻量自研注册表。代价是 Nacos 成为**有状态关键基础设施**，其配置存储必须持久化 + 备份，可用性按核心组件对待（本地/测试 standalone 内嵌 Derby；生产 MySQL 持久化）。

三条逻辑线（既定边界，不可逾越）：

| 线 | 职责 | 存储 | 能做什么 | 不能做什么 |
|---|---|---|---|---|
| 数据线 | 源数据采集 + 三库存储 + 迁移 | `raw_dingtalk` / `raw_wdt` / `mart_ops` | 只读源、写 raw/mart | 不含业务判断；不向源/RDS 写 |
| apps 线 | 数据管道后端服务（同步/投影/提取）+ 钉钉网关 | 驱动数据线 | 各守一条数据线的写 | 不产出业务内容；不跨线写 |
| 业务线 | 业务机器人（杭州/绍兴/渠道/榜单/告警） | 只读业务库 | 读业务库产出报表/告警/页面 | 不碰 raw、不碰源、不挂源凭据 |

Nacos（注册/配置中心）与控制面是与三条运行线**正交的基础设施层**：只承载配置、发现与生命周期，不读 raw、不碰源、不改业务数据、不存源凭据。

## 2. 容器清单

| 服务 | profile | 线 | 职责 | 连接 | 源凭据 |
|---|---|---|---|---|---|
| `nacos` | `nacos` | 基础设施 | 注册中心 + 配置中心 + console（8848） | 内嵌 Derby（dev）/ MySQL（prod） | 无 |
| `mysql` | — | 数据 | 三个 `_test`（及后续生产）库 + Nacos 持久化（prod） | — | — |
| `test-runner` | — | — | 只跑测试 | 三测试库 | 无 |
| `sync-dingtalk` | `live-sync-dingtalk` | apps | 钉钉侧只读源（AI 表 / 通讯录）→ `raw_dingtalk`（+mart 摘要） | raw_dingtalk + mart_ops + Nacos | 仅钉钉 |
| `sync-wdt` | `live-sync-wdt` | apps | WDT API → `raw_wdt`（+mart 摘要） | raw_wdt + mart_ops + Nacos | 仅 WDT |
| `project-mart` | `project` | apps | raw → `mart_ops` 投影 / `rebuild_projection` | 读 raw_* + 写 mart_ops + Nacos | 无 |
| `extract-mart` | `extract`（阶段 3） | apps | raw / `payload_json` → 业务 mart 明细 + 维度（`dim_calendar` 工作日、`dim_robot_member` 组织成员）（robot 的数据源，故前置） | 读 raw_* + 写业务库 + Nacos | 无 |
| `dingtalk-gateway` | `dingtalk-gateway`（阶段 4） | apps | 钉钉交互唯一入口：Stream 长连接（按群路由多 region）、报数直接落 `mart_ops`、群消息 / DING 投递；钉钉 AI 表不再作真源、不回写 | 写业务库 + Nacos | 仅钉钉 |
| `robot` | `robot`（阶段 4） | 业务 | 日报机器人一族：杭州/绍兴/渠道（cron，只读库算提醒/催办名单、榜单、渠道日报内容）；一镜像多子命令，多 region = 多份 Nacos dataId | 只读业务库 + Nacos | 无 |
| `pages-leaderboard` | `pages`（阶段 4） | 业务 | 榜单页面 | 只读业务库 + Nacos | 无 |
| `control-api` | `control` | 控制面 | 业务化门面：读 Nacos + 读 mart_ops + 托管前端 | Nacos + 读 mart_ops | 无 |

`mysql` 与 `test-runner` 维持现状。所有 `live-*`/`robot-*` 服务仍为显式 profile，不随 `docker compose up` 启动；`nacos` 作为核心可设为常驻。

## 3. Nacos 作为核心（注册中心 + 配置中心）

### 配置中心（关停/扩线 + 服务配置）

- **数据模型（已实现·阶段 2）**：`namespace` 按环境（dev/test 默认 public；prod 用 `test`/`production`，发布时自动建）；`group=PIPELINES`；**每条服务一个 dataId `{service_id}.yaml`**（如 `sync-wdt.yaml`），YAML 内容。
- 每条内容：`enabled`、`kind`（`apps`/`business`）、`sources`（如 `[wdt]`）、`schedule`、`reads`、`description`。
- **关闭**：容器启动时读到 `enabled=false` 即跳过（打印 `service=<id> status=skipped reason=disabled`），**不建 run、不连库**。
- **扩线**：新增 dataId（`publish-pipelines --if-missing`）+ compose 加 profile，即上线。
- **生效方式**：cron 型容器**启动时拉取**（当前）；长驻容器后续加 Nacos listener 推送。
- **宕机/缺省策略（fail-open）**：`Nacos → SDK 本地快照 → 版本受控种子 → 内置默认(enabled)`。注册中心任何异常都**默认放行**，报表不中断。
- **契约（防腐层）**：消费方只依赖 `ConfigSource`（`pipeline_config.py` 的 `get_pipeline(service_id)`），后端可插拔——现为 `NacosConfigSource`，另有 `FileConfigSource`/`StaticConfigSource` 供兜底与测试。
- **种子导入**：`python -m common.public_data.cli publish-pipelines --seed <seed>` 首启导入 Nacos（`--if-missing` 幂等）；此后 Nacos 为准，种子仅作兜底。
- **范围**：Nacos 存的是**管线注册表**（开关/源/调度），**不是** manifest。`source-manifest.json` 仍是版本受控的挂载文件（数据契约，由 `build_manifest.py` 生成）；注册表决定"是否跑/跑哪条源"，manifest 决定"同步哪些表"。

### 服务发现

- **尚未启用**（阶段 2 只落配置中心）。单机固定拓扑下发现用不上；契约层已备（`nacos-sdk-python` 已接入），待多实例/多机再开。

### 边界（关键）

- Nacos 只放**配置与发现**，**不放**：raw/业务数据、源凭据（钉钉/WDT 凭据仍为挂载文件，绝不进 Nacos 或 git）。
- 业务线容器仍只读业务库取**数据**；从 Nacos 只取**配置**。

## 4. 控制面中间层（`control-api`，FastAPI）

Nacos 已提供通用 console（配置/发现维护）。`control-api` 只做 **业务化门面**，不重复造 Nacos 的通用能力：

- 经 `nacos-sdk` 读业务线配置 + 读 `mart_ops.sync_runs/sync_dataset_summary`，聚合成「线状态」业务视图。
- 写（启停/扩线）经 Nacos publish API，**且落审计**（谁、何时、改了哪条线的哪个字段）——审计是 Nacos console 没有的，由 control-api 补。
- 托管 schema 驱动前端，供非运维同学使用（比裸 Nacos console 更聚焦业务线）。

接口（挂 `/api/v1`）：`GET /api/v1/schema`、`GET /api/v1/lines`、`GET /api/v1/lines/{id}`、`POST .../enable`、`POST .../disable`、`POST /api/v1/lines`。

## 5. 前后端一致性（硬指标）

1. **同一制品同时供 UI 与 API**：`control-api` 既出 `/api/v1/*` 又托管前端静态页，同一镜像同一版本。
2. **schema 单一源**：Pydantic → OpenAPI（`/openapi.json`）+ `/api/v1/schema`；前端按 schema 渲染、不硬编码字段，后端按同一 schema 校验。
3. **写路径唯一 + 版本化**：业务写经 `/api/v1`（落审计），版本挂 `/api/v1`。

配置的真源是 Nacos；UI 与所有业务容器最终都从 Nacos 派生，杜绝「文件改了但服务没读到」的漂移。

## 6. 运行与状态模型

- **run_id 按线独立**：`sync-dingtalk`/`sync-wdt` 各自独立 `sync_run_id`，故障隔离。
- 命名锁 `public-data:<source>:<dataset>` 按源隔离，并发不冲突。
- 跨库无分布式事务；`projection_pending` 由 `project-mart` 仅从本地 raw 重建。

## 7. 安全与边界约束

- 业务线与控制面容器：禁止 `raw_*` 连接、禁止源凭据、禁止外呼钉钉/WDT/真实 RDS。robot 已按此收敛——钉钉凭据与 Stream/群消息/DING 全部由 apps 线 `dingtalk-gateway` 承担。
- `sync-dingtalk` 仅挂钉钉凭据、`sync-wdt` 仅挂 WDT 凭据；敏感文件不入镜像/build context/git/Nacos。
- Nacos 生产开启鉴权；其持久化库纳入备份。
- 输出沿用非泄露约定：只报 run id、数据集名、数量、主键摘要、错误码。

## 8. 分阶段实施

1. **阶段 1（apps 线拆分）·已完成**：`cli.py` 的 `live-sync` 增加 `--source=dingtalk|wdt`；`live_sync.sync()` 支持单源；compose 增加 `sync-dingtalk`/`sync-wdt`/`project-mart`；run_id 按线独立。
2. **阶段 2（Nacos 接入）·已完成**：compose 增加 `nacos`（profile `nacos`，standalone）；`pipeline_config.py`（模型+`ConfigSource` 契约+Nacos/File/Static 后端）；`sync-*` 启动读配置做 `enabled` 门控；`publish-pipelines` 种子导入；`nacos-sdk-python==0.1.12`（零依赖）接入。
3. **阶段 3（提取层，前置）·进行中**：`extract-mart`，raw / `payload_json` → 业务 mart 明细 + 维度表（`dim_calendar` 工作日、`dim_robot_member` 组织成员）。robot 只读 `mart_ops`，故提取层必须先于业务线落地。前置依赖：通讯录读取权限（**已实测开通**，见 §10）。
   - **已落地**：`mart_extract_schema.py`（提取层 DDL + 投影**白名单**：作废列 `achievement_rate`、钉钉技术列与 `parent_record_refs` 一律不投影）、`live_migrations` 新增 `mart-extract-v1`（并把 `sync_dataset_summary.source_name` 扩为 `enum('dingtalk','wdt','extract')`，使控制面仍只有一个「线状态」入口）、`extract_mart.py`（`MartExtractService`：全量重放 raw → mart、`ON DUPLICATE KEY UPDATE` 幂等、run_id 按线独立、摘要以 `source_name='extract'` 落库、`dim_calendar` 整体替换）、CLI `extract-mart` + `live_safety.require_extract_run`（写库门控，**不需要 `--live-read`**）、`docker/integration/calendar.seed.json`（版本受控 rule，`restDays` 一律由 `generate_rest_days` 推导）、compose `extract-mart`（profile `extract`，**不挂任何凭据**）、`pipelines.seed.yaml` 新增 `extract-mart`。
   - **已落地（续）**：`dim_robot_member` 通讯录直连，链路按 §2 容器契约走全纵向——`sync-dingtalk`（持钉钉凭据）按 `org.seed.json`（版本受控「区域 → 顶层部门 ID」，键序即优先级、other 兜底排最后）递归展开子树 → `raw_dingtalk.dingtalk_org_member`（`raw-dingtalk-org-v1` 迁移，PK=user_id，「当前全集」按最新 run 圈定）→ `extract-mart`（零凭据）整体替换 `mart_ops.dim_robot_member`（raw 为空则跳过不清空）。manifest 新增 `dingtalk.org` 声明（与 sheet 模型分离，同考勤排班决策的理由）；声明了但缺网关/缺种子 = 显式失败。
4. **阶段 4（钉钉网关 + 业务线容器化）·进行中**：`dingtalk-gateway`（Stream 监听 + 报数落库 + 消息/DING，持钉钉凭据）+ `robot`（只读 `mart_ops`，cron 算名单/榜单/渠道内容）+ `pages-leaderboard`；配置存 Nacos、多 region 多 dataId、长驻容器加 Nacos 推送。计划：`docs/superpowers/plans/2026-09-11-stage4-robot-gateway.md`。
   - **已落地**：投递契约（§9 已定 = `robot_outbox` 表轮询）+ `mart-ops-outbox-v1` 迁移 + `OutboxRepository`（幂等入队 / 待投递 / 状态回写）+ `common/daily_robot/mart_tasks.py`（remind/check 文案逐字复用、名单走 `common.metrics`、幂等由 `region:kind:business_date` 唯一键承载、`stateFile` 职责退役）+ `common/gateway/` 投递器（轮询分发、群消息走 `DingTalkClient`、DING 保持 `dws` 命令契约、非泄露错误码回写）+ **容器与配置**：`robot-hangzhou`（profile `robot`，零凭据零外呼，`require_business_run` 门控）、`dingtalk-gateway`（profile `dingtalk-gateway`，仅挂钉钉凭据，`require_gateway_run` 门控——外发须 `--live-send`）、`common/region_config.py` + `regions.seed.json`（区域集合由种子定义、Nacos 只做字段覆盖、group `REGIONS`、fail-open）、`pipelines.seed.yaml` 登记两服务、Compose 合约测试把守凭据边界。
   - **待落地**：Stream 报数落库、cron 切换与旧路径退役、Nacos 真实 region 配置发布（占位符替换）。榜单已改读 `mart_ops`（`mart_leaderboard.py`：`mart_collect` 对拍 `collect`、`render_bc_markdown` 纯展示层、`mart_cli leaderboard` 入 outbox；`build_html` 吃 mart 视图就绪）。
5. **阶段 5（控制面）**：`control-api`（读 Nacos + 读 mart_ops + 审计）+ schema 驱动前端。

每阶段独立可验收。

## 9. 开放点（阶段 3+）

- **Nacos 部署**：dev/test 已用 `profile nacos` + standalone 内嵌 Derby；prod 是否用 `mysql` 做持久化库（需备份）或独立库。
- **服务发现**：单机暂不启用；多实例/多机时开启自我注册。
- **业务线调度**：沿用宿主机 cron 触发 profile（默认），还是 `scheduler` 容器从 Nacos 读 `schedule` 统一调度。
- **控制面审计**：`mart_ops.control_audit` 新表（默认）vs Nacos 配置变更记录 + 仅日志。
- **robot 的线归属与钉钉边界（已定）**：日报机器人**不再直连钉钉 AI 表、不持钉钉凭据、不写表**；钉钉 AI 表降级为**非真源**（逐步弃用）。链路：钉钉表 →（`sync-dingtalk`）→ `raw_dingtalk` →（`extract-mart`）→ `mart_ops` →（`robot` 只读）；报数 →（`dingtalk-gateway` Stream 收数）→ 直接落 `mart_ops`。
- **多 region 的 Stream 连接唯一性（已定）**：杭州/绍兴/渠道共用同一钉钉应用，而 Stream 按应用建连——由**单个 `dingtalk-gateway` 进程按群（conversationId）路由多 region**，不可每 region 各起连接（同应用多连接会争抢事件、串错 handler）。绍兴尚未接入（config 仅有 example、只建了组织档案），正是并入窗口。
- **`robot` → `dingtalk-gateway` 的投递契约（已定）**：`mart_ops.robot_outbox` **表轮询**，不选 gateway 内部 API。robot 只算「该发什么」并写 outbox 行（保持「世界 = mart_ops + Nacos」，不引入协议/鉴权耦合）；gateway 轮询 `pending` 投递并回写状态。outbox 行即审计；`dedupe_key = region:kind:business_date` 唯一键承载幂等，现行 `stateFile` 去重职责退役、robot 变无状态；gateway 重启不丢消息、重试上限 DB 可见。实现计划见 `docs/superpowers/plans/2026-09-11-stage4-robot-gateway.md`。
- **口径的放置位置（已定·模块已落地）**：DB 侧只做**结构性补全**（`dim_robot_member` × 日期范围 LEFT JOIN 事实表求"未填"组合）；**业务口径放共享 Python 模块**（`common/metrics/daily_report.py`），由 `robot` 与 `pages-leaderboard` 共同 import——口径单点在代码里、可单测、不污染 mart schema，避免"机器人一个数、看板一个数"。**已落地（2026-09-11）**：纯口径（`achievement_rate` / `elapsed_workdays` / `summarize_people` / `unfilled_members`，与既有三处实现逐位同口径）+ 结构性补全查询（`fetch_unfilled_members` 反连接等），21 例单测含 melt 陷阱与九月场景自证；消费方切换（robot 改调该模块）属阶段 4。
- **钉钉考勤排班的层级归属（已定）**：判定标准是「真源 or 校验源」——真源进数据源层（manifest + raw），校验源放 apps 层、不进 manifest。当前事实（业务组拉不到排班、`listbyusers` 已挂）下它是**校验源 → 放 apps 层**。为何不进 manifest、两条约束、以及将来升级为真源的固定改动清单，见 §10。

## 10. 数据口径与外部依赖（已定）

### 达成率：丢弃源列，改派生

- **口径（源码确证，可逐位复现）**：`达成率 = Σ(已过工作日 sales_amount) ÷ 月目标`。三处既有实现完全同口径——`core.recalc_totals`、`listener._calc_progress`、`leaderboard.collect`；且 `check_data` 以 `abs(rate - day_sum / target) <= 0.005`（0.5 个百分点容差）反向校验源表值。
- **单位零换算**：既有实现从分子到分母**均不做单位换算**，即日列值与月目标列**同单位**。派生必须照抄这一比值，**不得做单位归一**（如把 `sales_amount` 除 10000 会与历史值不一致）。字段名标注的"（万）"与展示层 `_fmt_wan` 按"元"格式化存在标签矛盾，属展示层问题，建议以一行真实数据核对数量级。
- **源列 `achievement_rate` 作废**：raw 层列保留（零迁移成本），提取层不投影；其值留作过渡期**双跑对照**基准。`recalc_totals` 的"重算 + 写回"整体删除。
- **melt 陷阱**：melt 后每一行都重复携带 `monthly_target`，分母必须取 `MAX(monthly_target)`，**不可** `SUM(monthly_target)`（会被 × 天数）。

### 工作日真源：保留日历并规范化（「钉钉拉业务组排班」已实测排除——**接口可用，是业务组没数据**）

**实测（2026-09-11，用应用凭据实调）**：

| 探测 | 结果 |
|---|---|
| 通讯录部门树（从 `rootDeptId` 递归） | ✅ errcode=0，43 人，与 `config.org.archives` **100% 一致** |
| `attendance/getusergroup`（逐人查组归属） | ✅ **40/43 人有考勤组**；其中 **38 人同属「业务组」`879530059`（type=`TURN` 排班制）** |
| `attendance/group/query`（组配置） | 「业务组」39 人、3 个班次（八点半／诸暨8点／诸暨9点）、4 个考勤地点；`work_day_list=[]` |
| `attendance/schedule/listbyusers` | ❌ **errcode=40035「不合法的参数」**——单人、换 `op_user_id`（含组主负责人 `095161066437872384`）、换参数键名（`userids`/`userid_list`）、换区间，均无法通过。**这是接口调用失败，不是「数据为空」**（初期曾误读为"零记录"，见下方教训） |
| `attendance/schedule/listbyday`（**查询某天的排班**） | ✅ **可用**。真实签名为 `user_id` + `date_time`（`op_user_id` 可传 `"0"`）；在其它排班制组返回 **100% 覆盖、含未来日期** —— 证明**接口/权限/参数全部正常** |
| `attendance/shift/query`（班次明细） | ✅ 业务组 3 个班次（八点半／诸暨8点／诸暨9点）均存在，可读到上下班时间与迟到规则 |
| 全部 **16** 个考勤组 | TURN 6 组 `work_day_list` 全空（排班制不适用周配置）；FIXED 3 组（各 1 人）为「7 天全上班」；NONE 3 组无班次 |
| 旧打卡结果 `attendance/listRecord` | ❌ `subcode=isp.top-remote-connection-error`（接口已下线） |
| 排班接口区间上限 | ⚠️ 单次查询跨度**不得超过 7 天**（超限 errcode=41041） |

**决定性对照实验（2026-09-11 实调 `schedule/listbyday`）**：

| 考勤组 | 类型 | 人数 | 9/9（过去·上班） | 9/15（未来·上班） | 9/16（未来·上班） |
|---|---|---|---|---|---|
| **业务组** `879530059` | TURN | 39 | **4/39** | **0/39** | **0/39** |
| 八点半考勤组 `482765099` | TURN | 17 | 17/17 | 17/17 | 17/17 |
| 万科九点考勤组 `629605105` | TURN | 18 | 18/18 | 18/18 | 18/18 |

同公司、同应用、同权限、同接口：**另两个排班制组满员且有未来排班**，业务组却是 4/39 且**未来日期全空**。
→ **排除接口、权限、参数的一切可能**，业务组在钉钉侧就是没有排班。

那 4 人是例外（班次为「八点半班次」「诸暨9点班次」，均属业务组 `shift_ids`），
推测为仍在钉钉单独排班的少数岗位；**不足以支撑 39 人的工作日口径**。

**结论：钉钉能拉排班，但拉不到「业务组」的排班**
1. 「业务组」的排班**不在钉钉**——原因已明确：**业务组排班比较特殊，接入「用友 T+」**，故 `work_date`／`is_rest` 取不到；
2. FIXED 组的 `work_day_list` 只能表达"每周固定工作日"，**表达不了大小休轮换**，且现有 FIXED 组都是"7 天全上班";
3. 故**考勤数据可信 ≠ 考勤承载工作日口径**。差异不在可信度，在**覆盖面与字段是否存在**；
4. **接口选型教训**：`listbyusers` 报 40035 属**调用失败**，与"返回 0 条"是**两种不同的失败模式，不可混读**；判定"数据是否存在"必须用**能跑通的接口**，并**设对照组**（本例正是靠"另两个 TURN 组满员有未来排班"才锁死结论）。

**口径认知已对齐（这也是现日历的来源）**：`restDays=[6,13,19,25,26,27]` 正是「大小休 + 法定节假日 + 调休」的产物——
- 9/6、9/13 休（**小休周**，周六 9/5、9/12 上班）
- 9/19 休、9/20 上班（**大休周**，被中秋调休占用）
- 9/25–27 **中秋法定假**

即现有日历已按"特殊行业大小休 + 法定节假日保障"编制，与业务口径一致。

**修正后的设计：用友 T+ 为长期真源，`restDays` 为现行代替（数据源占位已就位）**

业务组排班比较特殊，**接入用友 T+**。故工作日口径的**最终来源是 T+ 排班**；**接入前用 `restDays` 代替**。

| `calendar.source` | 语义 | 状态 |
|---|---|---|
| `local` | 随 config 下发的 `restDays`（由 `rule` 推导） | ✅ **现行，兜底** |
| `yonyou_tplus` | 用友 T+ 排班（按 `(year, month)` 取每日排班） | 🚧 **占位，未接入** |

- 占位类 `YonyouTplusSchedule.load()` 抛 `CalendarSourceUnavailable` —— **明确失败，不静默降级**；未知 `source` 直接 `CalendarError`，避免静默走错口径；
- **接入清单**：`baseUrl` / `appKey` / `appSecret` / `tenantId`(账套) / `schemeId`(排班方案)；按年月取数 → 映射休息日；加**按月缓存**与 **T+ 不可用时回退 `local` 并告警**；
- **切换时双跑对照**：历史达成率由 `restDays` 算出，不可与 T+ 口径混算。

**`restDays` 由规则生成，不手工列举**（`generate_rest_days`）：

| 规则 | 输入 | 语义 |
|---|---|---|
| 基准 | — | 每周日休息（**小休周**，周六上班） |
| 大休周 | `bigRestSaturdays` | 指定的周六一并休息（须为当月真实周六，否则报错） |
| 法定假 | `holidays` | 计入休息 |
| 调休 | `makeupWorkdays` | 从休息集合中剔除 |

**自证（2026-09）**：`bigRestSaturdays=[19]` + `holidays=[25,26,27]` − `makeupWorkdays=[20]` ⇒ `[6,13,19,25,26,27]`，与现行 config **逐位一致**。

- `restDays` 是**渲染结果**（消费方直接可用），`rule` 是**意图**；`check_rule_matches_rest_days()` 校验二者一致，漂移即告警；
- **年度节奏**：国务院公布次年节假日安排后，**HR 只需更新 `rule`**（大休周六 + 法定假 + 调休），`restDays` 自动重算；跨年未更新即告警。

**后续规范化（阶段 3+）**：`mart_ops.dim_calendar(business_date, is_workday, source, note)` 入库后，`listener._day_column`、`core.today_info`、`leaderboard` 三处统一改读该表，消除"各处各自读 config"的分叉。
- **可选校验源（非真源）**：打卡记录只能给"实际出勤"，不可当分母；且需另验新版打卡接口契约。

### 钉钉考勤排班的层级归属（已定）

**判定标准只有一个：它是「真源」还是「校验源」。** 真源进**数据源层**（manifest 契约 + raw 落库）；校验源放 **apps 层**即可，不进 manifest。

两条架构铁律先压掉可选空间：

1. **业务线不碰源（§7）**——robot 禁止挂钉钉凭据，「拉排班」这件事**无论如何都要在 apps 线**执行，不可能下放到业务线；
2. **raw 是唯一可重放层（§6）**——只有走 manifest→raw，才有 `rebuild_projection`（不重读源重建 mart）与 `sync_runs` 的口径回溯能力。

故真正的分歧不是「数据源层 vs apps 层」，而是**要不要为它付出「进 manifest 契约」的代价**：

| 归属 | 换来什么 | 代价 |
|---|---|---|
| 数据源层（manifest + raw） | 幂等 upsert、`sync_runs` 可观测、`rebuild_projection` 可重放、口径可回溯 | 每加一个源都要动 `manifest.py` 校验 + `SourceManifest` + raw 迁移 |
| apps 层（运行时能力） | 零契约改动、不进 `_KNOWN_TABLES` | 拉完即弃，无历史、无重放 |

**结论：按当前事实只能放 apps 层。** 业务组（唯一需要工作日口径的 39 人）在钉钉侧**没有排班**（见上节对照实验）；`listbyusers` 已挂（40035）；§10 已把打卡记录定为「可选校验源（非真源）」。**校验源不需要可重放**——它只产出差异告警、不参与任何计算（不可当分母），为它改数据契约是纯负债。

**两条约束（无论将来放哪层都成立）**：

1. **不塞进现有 `dingtalk.bases[].sheets[]` 模型**——那条路径是 **AI 表语义**（`validate_sheet`/`read_records` 按 base+sheet 读表）；排班是 **REST 按 (人,日) 扇出**（`schedule/listbyday` 实测签名 `user_id` + `date_time`，`op_user_id` 可传 `"0"`），还有**单次 ≤7 天**区间上限与上千次调用量，模型不匹配；
2. **校验逻辑不放 robot**——即使只是校验，也须在 apps 线执行（凭据边界），robot 只读 `mart_ops`。

**现在（校验源定位）的落法**：

- **位置**：apps 线，`extract-mart` 的一个**校验步骤**，或 `scripts/` 下的独立子命令（复用 `sync-dingtalk` 已有的凭据注入方式）；
- **取数**：`AttendanceReadGateway`（走 `schedule/listbyday` + 7 天窗口切片 + 按 `(user_id, date)`）；
- **产出**：**差异告警** → 落 `mart_ops` 对照记录，配合 `dim_calendar.source` 标注；**绝不覆盖** T+ / `restDays` 的口径；
- **不动**：`SUPPORTED_SOURCES`、`source-manifest.json`。

**将来若升级为真源**（某区域改以钉钉排班为准），改动点是固定一组：

| 位置 | 改什么 |
|---|---|
| `common/public_data/manifest.py` | `_KNOWN_TABLES` 加目标表；新增 `_load_attendance_datasets`；`SourceManifest` 加字段 |
| `docker/integration/source-manifest.json` | 新增 `attendance.datasets[]` |
| `common/public_data/live_sync.py` | 新增 `_sync_attendance_dataset` + `sync()` 分支 |
| `scripts/build_manifest.py` | 生成逻辑 |
| `extract-mart` | 投影 `mart_ops.dim_calendar` |

此时 `source=` **仍标 `dingtalk`**（凭据与 `raw_dingtalk` 同源，不必新增 source），隔离交给 Nacos 的**服务粒度**（`pipelines.seed.yaml` 加 `reads`/`schedule`）；仅当调度频率与 AI 表差异大到需要独立熔断时，才拆出 `sync-attendance` 容器——那仍属 apps 线。

**一句话**：校验源放 apps 层，真源才进数据源层；而它现在是校验源。

### 组织成员：走 API 直连（B 方案）·已实测可行

- 区域↔部门映射**已存在**，无需新造：`config.org.rootDeptId = 1050135497` + `org.depts{hangzhou/shaoxing/other}`。
- **权限已开通，实测通过**：`topapi/v2/department/listsub`、`topapi/user/listid`、`topapi/v2/user/get` 全部 errcode=0；递归部门树覆盖 **43 人**，与 `config.org.archives` **完全一致**（杭 24 / 绍 16 / 其他 3）。
- 现状 `org_sync.py` 读三份**快照文件**（cron agent 跑 `dws contact dept list-members` 生成）——该胶水的历史原因（缺权限）**已消失**，可直连。
- **改为应用直连通讯录 API**：`DingTalkClient` 增通讯录方法；`org_sync` 的 `inputs`（文件路径）降级为可选，保留作离线/测试兜底。
- **顺带修正一处隐患**：`config.org.depts` 把**父子部门混列**（如 `1050062512` 与其 3 个子部门并列），且为新设项目部**留了手工维护成本**。直连后应改为「region → 顶层部门 ID」+ **递归取子孙**，新部门自动纳入。实测印证：绍兴销售部直属仅 1 人，16 人分布在两个子部门（项目部 6 / 诸暨 9 + 直属 1）。
- 产出 `mart_ops.dim_robot_member(region, name, user_id, ...)`。
- **已落地（2026-09-11，apps 线侧）**：网关 `common/public_data/org_read.py`（仅上述三个只读端点）；链路为 `sync-dingtalk` → `raw_dingtalk.dingtalk_org_member` → `extract-mart` → `mart_ops.dim_robot_member`（extract 保持零凭据）。区域映射落实为 `docker/integration/org.seed.json`（递归取子孙，父子混列隐患随展开去重消除）；区域归属规则是**种子顺序优先**（other 含根部门、展开即全公司，故排最后）。`org_sync.py` 的快照兜底与 `members` 映射改写属业务线侧，随阶段 4 的 `robot` 容器切换。
