# 上云切换执行日志（2026-09-22）

> 续接自 `docs/上云推进续接提示词-2026-09-22.md` §A。时间戳：日志时间为北京时间，DB 时间为 UTC。

## T7 冒烟：断点修复与全链路跑通（13:47–14:02 北京）

### 修复验证耗时对比

| 项 | 修复前 | 修复后 |
|---|---|---|
| 钉钉 API 单次连接 | ~120s（IPv6 connect 卡死后回落） | **0.12s** |
| sync-dingtalk 全程 | 2.5h 零产出（卡死，PID 29749） | **110s**（29 数据集） |
| sync-wdt 全程 | 未跑到（凭据占位符，见偏差 2） | **67s**（145 数据集） |
| extract-mart 全程 | — | **4s** |

修复内容：三台 ECS（dops-app/.241、dops-ctrl/.194、dops-ci/.195）`/etc/gai.conf` 幂等追加
`precedence ::ffff:0:0/96  100`（IPv4 优先，可逆，未改代码）。

### 每轮 sync 的 sync_run_id / 状态 / 行数对照

| sync_run_id | 内容 | 状态 | 起止（UTC） | 产出 |
|---|---|---|---|---|
| 71649d4c-201d-4f21-acfa-66a041368970 | 卡死的 dingtalk run | failed / `ipv6_egress_stall`（人工结案） | 03:04 → 05:50 | 0 行 |
| **7a822d71**-a407-4fab-862e-635d45e42bf9 | sync-dingtalk 重跑 | **completed** | 05:50:26 → 05:52:16 | 29 数据集，org_directory 91 行 |
| eace2546-357c-4dbc-a106-99844ffcc9e7 | sync-wdt 首跑（占位符凭据） | failed / `source_read_failed`（已查明，见偏差 2） | 05:54:17（16ms 即败） | 0 行 |
| **384cebca**-e9ee-4d0b-bfc0-2fde31ea449c | sync-wdt 重跑（真凭据） | **completed** | 05:58:21 → 05:59:28 | 145/145 数据集 |
| **17b204df**-c722-4ea1-8d88-10f3400f9234 | extract-mart | **completed** | 06:01:39 → 06:01:43 | mart 四表见下 |

mart 核心表行数（验收值）：`dim_robot_member`=91、`fact_stockout_line`=7005、
`fact_order_line`=3905、`fact_refund_line`=109。

### 遇到的偏差（3 处，均已处置）

1. **pkill 自伤**：`pkill -f 'common.public_data.cli live-sync'` 的模式串出现在 SSH 自身
   远程 shell 的命令行里，把自己的 shell 一并杀掉，导致 dops-app 的 gai.conf 追加没执行
   （另两台成功）。复核发现后单独补执行。教训：远程 pkill 用 `pgrep -f '[l]ive-sync'`
   括号技巧或先查 PID 再 kill。
2. **wdt 凭据是占位符（新发现的断点盲区）**：`/opt/dops/live/source-credentials.json`
   的 dingtalk 段为真、**wdt 段三字段全是 26 字符模板占位符**（`cli.py:232` 占位符桩
   直接抛错 → `source_read_failed`，16ms 即败，run eace2546）。续接提示词只写了
   "凭据+manifest 在位（600）"，未校验 wdt 段真伪。处置：本地昨晚 30 天回补实战用的
   真凭据在 `e:/repos/credentials/source-credentials.trial.json`（sid/app_key/app_secret
   长度 10/14/65，无占位符特征），scp 上机后**只合并 wdt 段**进 live 凭据文件
   （dingtalk 段原样保留、权限维持 600、临时文件已删）。重跑即 145/145 全绿。
   **建议**：此条回填续接提示词 §A 环境事实段（wdt 凭据曾为空头的历史与真值出处）。
3. **sync-wdt 首跑留下一条 failed 记录**（eace2546）：属诊断过程产物，根因=偏差 2，
   已用真凭据重跑成功覆盖。按铁律 3 口径已"查明重跑"，未删除该记录以保留审计痕迹。

### 验收核对（对齐阶段4手册第 2 步）

- ✅ 正式 run 全 `completed`，无 `projection_pending`
- ✅ mart 四表有数（stockout 7005 / order 3905 / refund 109 / 成员见下）
- ✅ **三轮稳定性实测**（16:00–16:10 北京，run：r2 dingtalk `578bb88e` / r2 wdt `216140f0` /
  r3 dingtalk `231c75f3` / r3 wdt `b6967cd3`）：
  - 背靠背 r2→r3（隔 5 分钟）：174 个数据集仅 2 个漂移——`wdt_trade_detail_0019`
    +1（76→77，白天新订单，业务性）、`wdt_stock_specs_0023` −1（9→8，源端规格查询
    波动性，全天各轮均有个位数上下）。**digest=unknown 仅 stdout 摘要显示问题，
    DB 的 sync_dataset_summary.record_id_digest 有真值**
  - r1→r2（隔 3 小时）另见：trade_detail +N（业务增长）、org_directory 91→90
    （随后两轮稳定，判为一次真实人员变动，已重跑 extract 对齐，`dim_robot_member`=90，
    run `f18c8a82`）
  - 教训：digest 束比对用 `GROUP_CONCAT` 默认 1024 会截断，比对须逐数据集 JOIN diff
- ✅ **org.archives 口径勘误**：手册"dim_robot_member 与 org.archives 人数一致"不成立
  （91/90 全量通讯录 vs 43 人旧机器人授权名单，两个维度）。dim_robot_member 验收口径
  = org_directory API 读取数（91=91 首轮 ✅）；43 人授权名单属 `bi_authz_grant` 维度，
  阶段4 做 grant seed 时再校验。手册第 2 步与续接提示词已同步修正
- 历史 failed 记录（00904484 schema_drift、d29c6e79 source_read_failed、71649d4c
  ipv6_egress_stall、eace2546）均为今晨事故与诊断产物，非正式轮次

### 结论

**T7 全链路冒烟通过**（dingtalk → wdt → extract-mart 全 completed，三轮稳定性与
全量通讯录口径验收均关闭，见上）。方案 §3 第 7 步可标 ✅，剩第 8 步（阶段4切换）。

---

## B1 regions 真值闭环（19:00–20:30 北京）

**触发**：运维定调新建报数专用机器人、测试群验证后切群（不碰生产应用 HTTP 回调）。

- **新机器人 Stream 4 项实测全过**：长连建立（WSS ticket）、功能验证群 @接收
  （捕获 ocid/群名/发送人）、`reply_text` 互动回执、`groupMessages/send` 主动发。
  旧应用（餐饮/杭州/主应用）Stream 均未开通（401，现网走 HTTP 回调），新应用
  直接 Stream 起步零风险
- **7 群 ocid 全部 Stream 捕获**（登记表 `e:/repos/credentials/group-conversation-ids.json`）：
  功能验证群（=阶段4测试群）、万科&大莲花&团购（vanke）、君品雅院（junpin）、
  线下整体（offline_all）、渠道日报表群（qudao）、线下杭州（hangzhou）、
  线下绍兴（shaoxing）。**杭州 ocid 与现网 config 登记值逐字一致**（群级标识
  与机器人无关的交叉验证）
- **seed 补齐 6 区域**（98b6844，PR #2 CI 全绿合并）：新增 shaoxing/junpin/
  qudao/offline_all，键名对齐 org.seed.json；qudao/offline_all 键名与 deptOrder
  暂定，`_备注` 标待运维评审。test_region_config 断言同步更新，本地 1320 例绿
- **Nacos 发布**：`regions.values.json`（仓库外真源，dops-app /opt/dops/live/
  600 副本）经 `publish_regions_from_env` 发布 6 条 `region-*.yaml`
  （group=REGIONS），**回读逐字段比对 READBACK_OK**；seed 文件 scp 同步
  dops-app/dops-ci
- **全部 region 统一新机器人**（robotCode=appKey `dingnlvj…`）；tableUrl
  杭州/绍兴/junpin/offline_all 四处真值（绍兴表地址取自 shaoxing config.example
  的实 ID）、vanke/qudao 按设计无表占位
- **去 AI 表格方案成文**：`specs/2026-09-22-de-ai-table-db-single-source.md`
  （DB 唯一可行源；P1 新区域直接无表启动、P2 杭/绍双轨迁移、P3 表格下线；
  §7 含四阶段群公告 + 常驻 FAQ，公告零技术词可直接复制）
- 占位尾巴（不阻塞）：leaderboardUrl ×6、vanke monthlyTargets、qudao/offline_all
  键名与部门序评审；新区域业务流程接入调度属阶段4后排

**B1–B5 全部关闭。方案 §3 仅剩第 8 步阶段4切换本体（G1–G5 人工闸口 +
双跑窗口，非单日任务）。**
