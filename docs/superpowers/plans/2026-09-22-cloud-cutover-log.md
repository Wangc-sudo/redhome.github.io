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

---

## 2026-09-22 23:37 ①门禁收口+上机 完成 / ②gateway 容器化 待 C4（执行 agent 续写）

### ① 门禁代码收口+上机
- Dockerfile 修复：`docker/integration/Dockerfile` L12 → `ARG PIP_INDEX_URL` 注入镜像源（默认空=行为不变，本地/CI 零影响）
- 本地全量：`1336 passed, 39 skipped, 740 subtests passed`，0 失败（提示词口径 1356，实测以 pytest 输出为准，差异为环境 skip）
- commit `9724001`（chore/p1-sync，8 文件 +603/-3；选择性 add，WDT 脏文件与 `.vite/` 未动）
- scp → dops-app:/opt/dops/repo 8 文件（代码 4 + 测试 1 + Dockerfile + 文档 2），服务器核对 `ARG PIP_INDEX_URL` 在 L12 生效

### ② 镜像重建 + gateway 容器化
- 构建：`docker build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ -f docker/integration/Dockerfile -t dops-app:latest .` —— 成功，日志无 ERROR，`cryptography-50.0.1` 经阿里云源装入；镜像 ID `6becf182feec`（596MB）
- 停 venv gateway：`pgrep -af 'gateway.cli'` 确认后 `kill 45662`（按 PID 精确停，未用 pkill -f）
- 首跑失败（`status=failed code=gateway_error` 重启循环）。根因：app.env 中 `PUBLIC_DATA_CONFIG`/`PUBLIC_DATA_REGION_SEED` 等指向 `/opt/dops/config-local` 与 `/opt/dops/repo`，容器内无此路径，`load_settings` 校验文件存在性直接抛错
- 处置（偏差登记）：docker run 补两个只读挂载 `-v /opt/dops/repo:/opt/dops/repo:ro -v /opt/dops/config-local:/opt/dops/config-local:ro`。不改代码、不动 app.env，venv 回退能力保留
- 重起后验证：`RestartCount=0 State=running`，Stream WSS endpoint 建立（`wss-open-connection-union.dingtalk.com`），日志无 failed 行
- 【待确认 C4】绍兴群测试消息（运维人发），随后验证门禁通过 + 回执落库
- 回退命令备妥：`docker stop dingtalk-gateway && docker rm dingtalk-gateway`，再按原命令重启 venv gateway（nohup /opt/dops/venv/bin/python -m common.gateway.cli run --live-send --confirm-local-test-write --with-stream --live-read --source-credentials /opt/dops/live/gateway-credentials.json）

### ③ 第 8 步阶段4切换
- 未开始。前提：② C4 通过、容器稳定。届时按 `docs/阶段4切换派发提示词.md` 走 G1-G5（全部人工闸口），先答 Q1/Q2/Q3 + 第 0 步预检，G1 等运维宣布窗口开始

### ② C4 验收（23:39 北京）
- 运维绍兴群发测试报数消息（王城，123）
- Stream 捕获 → 门禁通过（admin grant + dim 档案）→ 回执落库
- 证据：`fact_daily_report_offline` 行 `region=shaoxing, responsible_person=王城, business_date=2026-09-22, sales_amount=123.0000, synced_at=2026-09-22 15:39:11 UTC`，`source_record_id` 前缀 `stream:shaoxing:`
- 容器态：`RestartCount=0 State=running`，累计 failed 行 0
- **② gateway 容器化闭环。venv gateway 已停，容器为唯一在跑 gateway（铁律 2 单进程口径保持）**

---

## 2026-09-22 23:50 ③ 第 8 步：Q1/Q2/Q3 + 第 0 步预检（止于 G1，等运维）

### Q1 现状确认（机器可查证部分）
- `robot_outbox`（RDS mart 库）：空表，近 7 天 `delivered=0` ✅ 新链路 robot/pages 未接流量
- Nacos（192.168.0.4:8848，tenant=`production`，共 33 条）：REGIONS 组 6 条 `region-*.yaml` 真值全在（hangzhou/vanke/shaoxing/junpin/qudao/offline_all）✅ 与 B1 闭环一致；PIPELINES 12 条、BI 15 条在列
- 现网桌面端 cron 5 任务：**本机查无**（本机无相关计划任务，`e:/数字化/钉钉` 不在本机）→ 旧链路在另一台现网桌面机，**待运维确认仍启用**
- `docker compose -f docker-compose.integration.yml config --quiet` → OK（dops-app 上验证）

### Q2 凭据就位（事实锚点，待 G2 运维目视确认）
- dops-app `/opt/dops/live/`（600）：gateway-credentials.json、regions.values.json、source-manifest.json 副本
- 本机仓库外：`e:/repos/credentials/source-credentials.json`

### Q3 窗口约定
- **待运维约定**：本窗口覆盖范围（第 4 步观察 2 个工作日、第 5 步回退窗口 1 周，必然跨窗口）

### 偏差登记（文档 vs 现实，以现实为准，等裁决）
- D1：checklist 第 0 步预期「Nacos 无 region 真值/仍为占位符」，现实是 **6 条真值已发布**（B1 提前闭环，回读逐字段 READBACK_OK）→ checklist 第 1 步「发布真值」实质已完成，剩余动作 = G2 运维目视确认真值内容
- D2：checklist 写于上云前（localhost Nacos、mart_ops、Windows 本机 compose），现实链路已全在云（dops-app Docker、RDS mart、内网 Nacos tenant=production），命令形态需按云上实况折算
- D3：本地仓库在 `chore/p1-sync` 且工作区有 WDT 脏文件（B4 未收口），与「main 最新、git status 干净」口径不符（B3/B4 既定待办，不阻塞本窗口）
- D4：「已通知群成员双跑提醒」为人工事项，待运维确认

### G1 闸口：预检结果如上，等运维宣布「窗口开始」后进入 checklist 第 1 步（现实折算：G2 真值目视确认）

### 运维确认（23:5x 北京，Q1 余项 + 新指令）
- 运维确认：现网桌面机 cron 5 任务仍在启用（Q1 全项闭环 ✅）
- 运维新指令：现网桌面机 cron「可以关闭」，桌面 Docker「也可以关闭」
- **顺序风险（待裁决）**：robot-hangzhou / pages-hangzhou 云容器尚未启动（checklist 第 4 步动作）；若先关桌面 cron/桌面 Docker，18:30 提醒 / 20:00 催办+DING / 次日 8:30 榜单将断档；且 hangzhou 区域未经真群验证（C4 仅覆盖 shaoxing）

---

## 2026-09-23 00:10 架构修正：dops-app 改容器组（代码挂卷，迭代不重构镜像）

### 运维裁决（架构）
- dops-app 不得是单容器；改为**容器组**（compose 编排），代码不烤进镜像
- 迭代流：本地改 → 全量绿 → commit → scp `/opt/dops/repo` → `docker compose -f /opt/dops/docker-compose.dops.yml restart <svc>`；仅 requirements.txt 变更才重建镜像（`--build-arg PIP_INDEX_URL`）

### 落地
- 编排文件：`/opt/dops/docker-compose.dops.yml`（服务器本地，未入仓——待 B4 收口时一并决策），服务：`gateway`（dingtalk-gateway）+ `scheduler`（dops-scheduler），均 host 网络、`TZ=Asia/Shanghai`、`/opt/dops/repo` 只读挂卷为工作目录、凭据只读挂载
- 原单容器 gateway 已替换进容器组；新增 `dops-scheduler`（`python -m common.public_data.scheduler`，12 条注册管道 30s 轮询，MySQL 命名锁防重）

### Nacos 变更（可逆，PIPELINES 组 enabled 机制）
- `sync-runner` → **disabled**（与 sync-dingtalk/sync-wdt 同 02:00，防三 sync 并发撞限流，铁律 2）
- `robot-vanke` / `pages-vanke` → **disabled**（超出本窗口范围，防万科群未通知收到新样式提醒）
- 保留启用：sync-dingtalk(02:00)、sync-wdt(02:00)、extract-mart(04:00)、robot-hangzhou(18:00/20:00)、pages-hangzhou(08:30)（时间均为北京时）

### 代码修复（铁律 4 全流程）
- `scheduler.py` main() 错误导入（`load_settings` 在 `cli.py`，main 标 pragma no cover 从未被执行，容器组首跑暴露）→ commit `0aff304`，全量 1336+740 绿 → scp → 仅 restart scheduler 生效（**首次实战验证新迭代流，零镜像重构**）

### 验证
- `dingtalk-gateway` / `dops-scheduler` 均 `RestartCount=0 running`，容器内 `date` = CST；gateway Stream 重连正常；scheduler 启动行齐、`project-mart` 按设计跳过

### 风险与待办登记
- D5：`robot-hangzhou` cron 为 18:00/20:00，旧链路为 18:30 提醒/20:00 催办——新链路 18:00 即触发 `once`（按小时路由 remind），双跑期两链路提醒相差 30 分钟
- D6：**绍兴提醒无接替管线**（seed/Nacos 无 `robot-shaoxing`，`scheduler._COMMAND_TABLE` 亦无）——关桌面 cron 后绍兴 18:30/20:00 提醒断档；补建需走改代码流程（scheduler 命令表 + seed + Nacos）
- D7：今晚 02:00 起 sync-dingtalk/sync-wdt 首次自动触发，明早核查 `sync_runs`（铁律 3：failed/projection_pending 即验收失败）
- 杭州真群验证：等运维在杭州群发测试报数（同 C4 口径）

### 杭州真群验证（00:10 北京确认）✅
- 运维 23:53 在杭州群发测试报数（王城，12313）
- 落库行：`region=hangzhou, responsible_person=王城, business_date=2026-09-22, sales_amount=12313.0000, synced_at=2026-09-22 15:53:51 UTC`，前缀 `stream:hangzhou:`
- 门禁通过 → 回执落库；**杭州/绍兴两大真群均已验证，报数接收侧容器化全闭环**
- 「先备后关」前置全部满足：杭州验证 ✅、robot/pages 调度已就位（scheduler 容器）、双跑提醒差异已登记（D5）、绍兴缺口已登记（D6）

### 旧链路机器关闭（00:2x 北京，运维执行）
- 旧链路实际落点：阿里云 `39.101.76.87`（非资源评估文档中的 ECS-A/B，登记偏差 D8）
- 运维已关机；验证：`39.101.76.87:22` 不可达 ✅；天翼云两容器 Up、Stream 长连在 ✅；新链路对阿里云零依赖 ✅
- 时间门证据（今晚）：18:30 旧提醒不再出现、20:00 旧催办不再出现，仅新链路 18:00/20:00 输出
- **绍兴/万科提醒断档即时生效（D6 落地）**：运维选择先关后补 → robot-shaoxing 补建升为最高优先级待办；robot-vanke 是否重启用待运维确认

---

## 2026-09-23 00:35 robot 覆盖补齐到全部 6 区域（运维裁决 Q1/Q2）

### 运维裁决
- Q1：robot 功能覆盖所有群，缺的补进去 → 执行完毕
- Q2 事实答复：缺口不止绍兴——管线注册仅有 hangzhou/vanke，缺 shaoxing（回归性断档）+ junpin/qudao/offline_all（新增能力）；符合架构设计（种子定义区域集合 ✅6 区域早已齐，仅补管线注册）

### 执行（铁律 4 全流程）
- commit `c18212a`：`scheduler._COMMAND_TABLE` + `pipelines.seed.yaml` 各补 4 条 robot-*（0 18,20 * * *）；全量 1336+740 绿；scp 两文件上机
- Nacos：发布 robot-shaoxing/junpin/qudao/offline_all（enabled=true）+ 重启用 robot-vanke/pages-vanke，逐条回读确认
- `compose restart scheduler` → **16 条注册管道**上线，RestartCount=0（又一次零镜像重构迭代）
- 生效面：今晚 18:00/20:00 六个群（hangzhou/shaoxing/vanke/junpin/qudao/offline_all）全部收到新链路提醒；D6 绍兴断档就此闭合
- 登记：junpin/qudao/offline_all/vanke 四群为首次收到新样式提醒（运维已知悉爆炸半径）；pages-* 新区域未注册（榜单 HTML 无发布通道，QW Pages 随桌面端关闭，待 OSS/CDN 决策，不影响 robot 提醒）

### G4 闸口闭环（00:4x 北京，运维亲手执行）
- 旧链路整机 `39.101.76.87` 已由运维关机（cron 5 任务 + 桌面 Docker 随机停止），22 端口不可达已验证
- 无残余项；旧链路保持「可回退」而非「已删除」——关机即回退保底
- **回退窗口开始：2026-09-23，建议至 2026-09-30（1 周）**
  - 回退动作：阿里云控制台开机 `39.101.76.87`（cron/Docker 随机器恢复）+ 必要时 `docker compose -f /opt/dops/docker-compose.dops.yml stop` 停新容器组
  - G5（旧路径退役/checklist 第 5 步）须等回退窗口结束且无回退发生

### 报数解析器智能化上线（00:55 北京，commit `6e77fa5`）
- 运维裁决：A 模糊补全 + B 个性化提示 + C 单字指标容错全做；自动补零/代填不做（审计红线）
- A：门店词前缀互含+候选唯一自动识别（莲荷→莲荷里体验馆），回执回显（识别：原词），识别错用重报覆盖纠正
- B：格式提示按发送人部门个性化（本店成员给本店示例、根部门给门店列表）；其他区域文案不变
- C：团/零 单字指标带后随数字守卫（防零食/团队误判）
- 全量 1343 例绿（+7）；scp + `compose restart gateway` 生效，零镜像重构；容器 RestartCount=0、Stream 重连正常、本地与服务器文件 md5 一致

### /辅助指令族上线（01:16 北京，commit `bf68774`）
- 运维定稿 5 指令：`/帮助`、`/未填`（多门店区域按板块格、其余按成员，同 18:30 口径）、`/我的`、`/门店`、`/补签 姓名 [日期] 金额`（仅 bi_authz_grant admin，最长 30 天，署名=目标成员+回执注明代录人，非工作日拒签）
- 未识别 `/` 指令落帮助菜单防误录；`/补签` 优先于报数解析分发
- 全量 1362 例绿（+19）；scp + `compose restart gateway` 生效，RestartCount=0、Stream 重连正常
- 已知边界登记：补签在 DB 行无独立操作人列，代录留痕在回执与容器日志；如需 DB 级审计列另开任务

### 互动卡片菜单 PoC 部署（01:42 北京，commit `7826188`）
- 运维批准 PoC；官方文档查证：互动卡片按钮 `actionType=request` 支持 Stream 回调（零入站）
- 实现：`/菜单` 发按钮卡（SDK 内置公共模板 1366a1eb，免搭建）；按钮 `privateData.aux` 路由；卡片回调与报数同连接注册；查询类四指令白名单；结果就地更新卡片
- 全量 1377 例绿（+13）；三文件 scp + restart gateway，RestartCount=0、Stream 重连正常
- 待实测：功能验证群发 `/菜单` → 点按钮 → 看回调是否到达（按钮 request 动作在公共模板上的确切行为为 PoC 验证点；日志会打印回调原始键名）

---

## 2026-09-23 02:11 故障：三台 ECS 全体失联（双因叠加）与恢复

### 经过
- 01:4x 起 dops-app/dops-ctrl/dops-ci 22 端口全体超时、ping 不通；本机出站正常；tracert 断在电信骨干 221.229.38.90
- 根因（双因叠加）：① dops-app 宿主 02:01 异常重启（up 时间证实，疑平台事件）；② **办公网公网 IP 从 125.118/125.121 段漂到 36.20.86.193**，三台安全组 22 白名单全部不命中（第二次咬人，P1.3 已改过一次段）
- 恢复：ctyun-cli `CreateVpcSecurityGroupIngress` 为三组各加 2 条入方向规则：TCP22 ← 36.20.86.193/32（办公网当前 IP）+ **47.114.55.158/32（阿里云 ECS-A 固定 EIP，作永久跳板治本）**；sg 规则 ID 已留 CLI 返回
- 自愈验证：容器组 `restart: unless-stopped` 生效，宿主重启后 gateway/scheduler 自动拉起；Stream 重连正常
- 排障版 stream_handler（入站可见性日志，commit `9e5b9bf`）补完上机

### 教训与待办
- 动态 IP 是复发风险：今后管理通道优先走固定跳板 47.114.55.158（ssh -J）
- 宿主 02:01 重启原因待查（天翼云控制台事件中心/云监控）
- 机器人业务面在故障期间：宿主重启约数分钟内报数接收中断（出方向随实例恢复即复）

### 提示现代化 + /门店管理员覆盖 + 零宽加固（02:3x 北京，commit `9cf4700`）
- 运维反馈两条实群回执：①裸格式提示未跟上指令时代 → build_format_hint 统一附 /帮助 /菜单 入口；②`/门店` 在无多门店区域对管理员也应列出 → admin 跨区视图（标注），非 admin 不变
- 加固：指令/菜单解析剥离零宽字符（疑为 /菜单 首次无响应根因之一），@剥离兼容连写
- 全量 1380 例绿（+5）；上机 restart，RestartCount=0

### 机器人改名（02:4x 北京，commit `40d550c`）
- 运维实报：旧机器人「提醒事项」已下线，现网为「@日报小机器人」——旧名文案会引导群成员 @ 已下线机器人，消息零投递（此前部分"没反应"的根因之一）
- 新链路全部用户可见文案改名：build_format_hint、18:00 提醒、20:00 催办、DING 正文、卡片菜单标题；旧链路退役代码不动
- gateway + scheduler 双容器重启生效（提醒文案走 scheduler 的 robot 管线）

### 万科群数据格裁决 + 三视图上线（commit `af6701b`）
- 运维问：万科群"没有门店"，应用 万科/大莲花/日/周/月数据一类——**裁决（运维授权）**：数据格维持 门店×{零售,团购}（通讯录部门即门店=现成权限锚点；口语别名 万科→万科体验馆、大莲花→莲荷里体验馆 已覆盖叫法）；日/周/月按 Q1 定为**查询口径**
- `/我的`、`/门店` 升级 今日/本周/本月 三视图：本周=周一至今自动求和，本月=累计/目标/完成率；整格无数据才显示"暂无数据"
- 全量 1382 例绿（+3）；gateway 重启生效

### 互动卡片 PoC 终审（03:1x 北京）：免搭建公共模板路线判定不通
- 实证链：发卡 API ✅（Card.Instance.Write 权限开通后 200）→ 卡片群内可见 ✅ → **点按钮零效果、Stream 零回调** ❌
- 根因：SDK 内置公共模板 1366a1eb 的 msgButtons 仅支持 URL 跳转（{text,url,iosUrl,color}），action/privateData 被忽略，点击不产生回调事件
- 已验证可用的资产：发卡链路、卡片回调 topic 同连接注册、合成回调全链路（解析→路由→执行→PUT 更新卡片）、card_menu.py 全部代码与测试
- 结论已写入长期记忆（knowledge_id 86170070）；若重启此功能走卡片平台自定义模板（按钮交互=回传请求+privateData），仍零公网入站
- 兜底：文本 /辅助指令族为完整能力，不依赖卡片

---

## 2026-09-23 04:2x 万科 AI 日报表接入 + 空壳 contract 事故排除

### 万科日报表（统一数据源意图落地）
- 运维定稿：对齐现有格式（零代码抽取）+ 保留双入口（机器人/表格同键幂等）
- 建表：Base「万科&大莲花&团购日报表」+「9月」表 36 字段（项目部/责任人/日均目标/达成率/9月销量目标（万）/补充说明/1日~30日）+ 4 数据格行（店×零售/团购）
- 过程坑：dws CLI（node 启动器 + Go 本体）在 Windows 下 CJK argv 编码损坏 → 废表删除后直调 vendor/dws.exe 解决；第 2 批 15 字段（10日~24日）创建失败 + API 回读延迟 → 逐字段补建，终验 36/36 零乱码
- 注册：服务器真值清单 `source-manifest.json` 加 `daily_report_offline_vanke`（melt/inject region=万科&大莲花&团购，已备份 .bak-20260923）；`extract_mart._REGION_KEY_BY_DISPLAY` 补映射（commit `b0f6662`）；Nacos/live/seed 三处 tableUrl 占位→真表链接
- **E2E 实锤**：表内写 万科体验馆·零售 23日=100 → sync（records_read=1）→ extract → 事实行 `(vanke, 万科体验馆·零售, 万科体验馆, 2026-09-23, 100, 目标0)` ✅（验证值，运维可自行清除或覆盖）

### 空壳 contract 事故（sync 失败的真根因）
- 现象：手动 sync-dingtalk 连续失败 `source_read_failed`，12ms 速败
- 根因链：app.env `PUBLIC_DATA_CONFIG` 指向 67 字节**空壳 contract.json**（bases:[]，初始化占位从未切换）→ 空清单零 sheet → started→completed 被判非法转移（状态机要求经 raw_committed）→ 误标 source_read_failed。`PUBLIC_DATA_LIVE_MANIFEST_PATH` 为 compose 时代约定，裸跑代码不读；T7 系 inline 覆盖运行故未暴露
- 修复：app.env `PUBLIC_DATA_CONFIG=/opt/dops/live/source-manifest.json`（已备份 app.env.bak-20260923）+ 容器组重启 → sync-dingtalk 全 14 base completed
- 登记：转移校验 ValueError 被映射为 source_read_failed 属误导性 failure_code（已知问题，待改进）
- **今晚 02:00 自动 sync 缺失**：宿主 02:01:03 重启，scheduler 60s 宽限差 3 秒未补触发（设计如此）→ 已手动补跑 sync-dingtalk（f6c0e7f7/289dbb8a）与 sync-wdt（257a21ba）双源，extract-mart 04:00 正常

### 万科表迭代（运维结构调整 + 通讯录关联）
- 运维在 9月表 加 `销售路径`/`单选` 字段；已按群名与旧配置 projects 口径为 4 数据格行补填 销售路径（万科体验馆·零售→万科、万科体验馆·团购→团购、莲荷里体验馆·零售→大莲花、莲荷里体验馆·团购→团购）
- 新建「人员」表（不破坏 9月 同步契约）：姓名/部门/销售路径(singleSelect 万科·大莲花·团购)/角色，写入 dim_robot_member vanke 在册 6 人（张燕芳/陈楠/陈陈@万科体验馆、徐春燕/林燕山@莲荷里体验馆、吴金澎@体验中心根部门）
- 「已有的数据」核查结论：mart 万科事实行仅链路验证值 100（已在表内）；旧餐饮配置万科表为待填占位，base 搜索无带数据旧表——若运维另有所指需提供表链接

### 万科群聊记录回填（运维提供 18 条历史报数，05:2x 北京）
- 运维裁决：日期=第1~5轮→9-19~9-23；同格多人=最后覆盖；未写=0；宣蕾凤两条跳过（非在册，门禁本拒）；验证值 100 同步清零
- 执行：9月表 19日~23日 四格写入 → sync（vanke records_read=20）→ extract（daily_report_offline 835 行重投影）→ 事实行 20/20 与口径逐行一致 ✅
- 连带修复：运维将「责任人」改名「负责人」致格名字段丢失 → 补写 4 行负责人格名 + 清单 field_mapping 改键（同步链路恢复）
- 覆盖冲突登记（待运维复核，一句话可改）：9-19 万科·团购 张燕芳 10620 被陈楠同日 团购/0 覆盖为 0；9-19 莲荷里·零售 林燕山 556 被徐春燕同日 0 覆盖为 0
- 宣蕾凤预警：她若再报数将被门禁拒（⛔ 非责任人）；如需她报，先补 dim_robot_member
- 9-19/9-20 为周六/日（零售日报含周末口径，按运维指令照填）

### 万科群聊天记录回填（运维供源，05:3x 北京）
- 口径：保持店×指标；日期 第1~5轮→9-19~9-23（第5轮两条 @日报小机器人 必为今日）；同格多人最后覆盖（线上语义）；未写格=0；验证值 100 已归零
- 回填：9月表 4 格 × 5 天 = 20 单元格 → sync（records_read=20）→ extract（835 重放）→ mart 20 行终验一致 ✅
- 运维结构调整处置：责任人字段被删除新建为「负责人」（格名丢失）→ 已补写 4 行格名 + 清单 field_mapping 改键（责任人→负责人）
- 待纠错项（一句话即改）：① 9-19 张燕芳 团购10620 被陈楠 0 覆盖（丢失）；② 9-19 林燕山 零售556 被徐春燕 0 覆盖（丢失）；③ 宣蕾凤 团购4380（9-23）未录入（不在 dim 通讯录快照，归属未定）；④ 9-20/21 为周末按填报照录

---

## 2026-09-23 14:0x nginx 提前拉起：榜单页面发布通道闭环（运维指令，赶 8:30 榜单任务）

### 背景
- 运维指令：把 nginx 提前拉起来，8 点档有销售完成率榜任务。缺口即此前登记的「pages-* 榜单 HTML 无发布通道（QW Pages 随桌面端关闭，OSS/CDN 待决策）」——nginx 静态直出作为临时通道顶上。

### 落地（全部服务器侧配置，零代码改动）
- **页面生成**：手动各跑一次 pages 管线预热（容器内 exec，与 8:30 调度同命令）：
  `pages-hangzhou` → `/opt/dops/pages-output/hangzhou.html`（25 人）、`pages-vanke` → `vanke.html`（4 人）
- **nginx 入容器组**：`/opt/dops/docker-compose.dops.yml` 追加 `nginx` 服务（nginx:1.27-alpine，host 网络，unless-stopped），
  站点配置 `/opt/dops/nginx/pages.conf`（`pages-output` 只读直出、`Cache-Control: no-store` 防旧榜、裸路径 302 到 hangzhou.html）；compose 已备份 `.bak-20260923`
- **端口裁决（偏差登记）**：规划 80/443 对外，实测 **80 安全组 0.0.0.0/0 全放（规则 9-21 已在）仍不通**——天翼云对未 ICP 备案的 80/443 运营商层拦截；临时改 **8300**（对齐旧榜单服务端口约定），SG 规则 `sgrule-s8iqmo44oo`（TCP 8300 ← 0.0.0.0/0，规则 ID `sgrule-lu8f4b2fxm` 的 80 重复规则一并保留）。备案完成后切回 80/443
- **leaderboardUrl 真值回填**：`/opt/dops/live/regions.values.json`（已备份 `.bak-20260923`）hangzhou/vanke → `http://203.205.91.241:8300/{hangzhou,vanke}.html`，`publish-regions` 发布 6 条，Nacos 回读命中 ✅（其余 4 区域仍占位，pages-* 管线未注册，符合现状）；gateway 已 restart 刷新

### 验证
- 本机容器内 `curl 127.0.0.1:8300/{hangzhou,vanke}.html` → 200；办公网公网 `curl http://203.205.91.241:8300/hangzhou.html` → 200（`<title>杭州销售完成率榜单 · 9月</title>`，30566 字节）
- Nacos 三管线核对：pages-hangzhou / pages-vanke / channel-daily-qudao 均 `enabled: true`、`schedule: 30 8 * * *` —— 明早 8:30 自动再生页面，nginx 即时可见

### 待办登记
- D9：仓库内新增 `docker/integration/nginx/pages-cloud.conf` + compose 片段（服务器已生效的副本），随 B4 收口决策入仓走 PR；`docker-compose.dops.yml` 服务器本地编排同案处理
- D10：80/443 需域名 + ICP 备案后切回；届时 leaderboardUrl 同步改域名 HTTPS
- 8300 为无鉴权公开静态页（内部销售数据，曝光面=知道 IP:端口者），与既定 80 公开规划同级；如需收紧改办公网白名单但手机钉钉访问会受限，待运维裁决

### 续：pages-shaoxing / pages-qudao 接入（14:2x 北京，运维追问「绍兴与电商的呢」）
- 铁律 4 全流程：`scheduler._COMMAND_TABLE` + `pipelines.seed.yaml` 各补 2 条 pages-*（30 8 * * *）→ 全量 1388 例绿 → commit `c2887f0`（chore/p1-sync）→ scp 两文件（md5 一致）→ `compose restart scheduler`
- Nacos 增量发布用 `publish-pipelines --if-missing`（published=2），**刻意不整种子重发**——避免覆盖运行时调整（sync-runner disabled 等）
- 手动预热生成：shaoxing.html（12 人）/ qudao.html（23 人）；公网 200（`<title>绍兴销售完成率榜单 · 9月</title>` / `渠道日报销售完成率榜单 · 9月</title>`）
- leaderboardUrl 回填 shaoxing/qudao 并发布（6 regions），回读四区域全真值；gateway restart 刷新
- 现状：hangzhou/vanke/shaoxing/qudao 四区域榜单页全通；junpin/offline_all 仍占位（无 pages 管线，同法可补，待运维需要时再加）

## 2026-09-23 06:4x RDS 数据拉平：本地 MySQL → RDS（raw_wdt 30 天回补窗口）（执行 agent 续写）

### 背景与范围裁定
- 9-21 晚本地做过 WDT 30 天回补（真凭据 `source-credentials.trial.json`），数据落在本机 compose 库 `raw_wdt_test`；云上 T7 sync-wdt 只按短窗口重拉（RDS wdt_records 仅 9,447 行、窗口 09-21~09-22），**30 天历史缺口实锤**
- 盘点对照（本地 vs RDS）：fact_order_line 74,607(08-22~09-21) vs 3,969(09-20~09-22)；fact_stockout_line 75,606 vs 7,005；fact_refund_line 2,079 vs 109；raw_wdt.dim_product 3,257 vs 0
- **范围 = 仅 raw_wdt**（wdt_records + dim_product）：raw_dingtalk 云上每日 02:00 从钉钉 AI 表全量重拉且更新（org_member 91>本地 49），不搬；mart 不搬——三大 wdt fact 是 DELETE+全量重建（`extract_mart.replace_table`），拉平 raw 后云端 extract-mart 自动补齐；daily_report_offline/channel 走 upsert，Stream 直写行无删除路径

### 执行（全部经 dops-app 跳板，RDS 仅 VPC 内网）
1. 本地容器 mysqldump（`--insert-ignore --complete-insert --no-create-info`），**过滤 2 行测试残留**（sync_run_id ∈ {idempotent-full-run-b, isolation-test-run-0001}，窗口 2026-01-01 为测试桩特征）；805MB → gzip 43MB → scp 上机
2. 写前 CTAS 备份（一键回退）：`raw_wdt.wdt_records_bak_20260923`(9,447)、`raw_wdt.dim_product_bak_20260923`(0)、`mart_ops.fact_{order,stockout,refund}_line_bak_20260923`、`mart_ops.dim_product_bak_20260923`
3. pymysql 流式执行 63,045 条 INSERT IGNORE（134s）：wdt_records 9,447 → **129,717**（5,277 行云上已有窗口重叠被 IGNORE，云上行保留）；dim_product 0 → **3,257**
4. 云端手动 `cli extract-mart --confirm-local-test-write`（06:3x，避开 02:00/04:00 调度，run_id `cf654a77-0a1f-4154-ae6f-6e494b6cd913`）：12 数据集全 completed
5. 临时文件（服务器/容器/本机 dump 与脚本副本）已全部清理；脚本留存 `e:/repos/tools/{rds_inventory.py, rds_level_load.py, rds_verify.py, dump_wdt.sh}`

### 验证（拉平后 RDS 实测）
- fact_order_line **78,467**（08-22~09-22，= 本地 30 天 ∪ 云上近两日新增）、fact_stockout_line **75,606**（08-23~09-21）、fact_refund_line **2,079**（08-23~09-21）、raw_wdt.dim_product / mart dim_product **3,257**
- wdt_records 窗口 08-22 06:36 ~ 09-22 00:08，**无 01-01 测试窗口**（过滤生效）
- fact_daily_report_offline/channel_daily_sales upsert 语义未动（records_read=0），Stream 报数行（杭州/绍兴 9-21/9-22）抽查在位 ✅
- sync_runs 近期全 completed；历史 8 条 failed 为既有审计留存（含 eace2546，见前文偏差 3），非本次产生

### 偏差登记
- D11：mysqldump 8.4 输出 `INSERT  IGNORE`（双空格），首版加载器按单空格过滤执行 0 条——因祸得福暴露了测试残留问题，重导出后才真正导入
- D12：dump 语句无库名前缀，pymysql 连接须显式 `database=raw_wdt`（1046 报错后修复重跑，事务回滚无副作用）
- D13：fact_daily_report_offline 两次盘点 1,240 → 1,237（-3）：extract 对该表 0 读 0 写无删除路径，归因于活动系统实时写入（运维自清 vanke 验证行等），留档观察
- D14：备份表 `*_bak_20260923` 共 6 张暂留 RDS，观察 1 周无异常后由运维 DROP

---

## 2026-09-23 15:4x 线下整体销售自动汇总上线（方案 P1-P3 一次到位，运维裁决「不节约 token、逻辑一次对齐」）

### 王城测试数据清理（前置）
- `fact_daily_report_offline` 删 3 行 stream 测试行（hangzhou 9-22/12313、9-23/123、shaoxing 9-22/123，source_record_id 前缀 `stream:`，删前列出+删后核对 0 残留）；hangzhou/shaoxing 榜单页已重生成（25→24 / 12→11 人）

### P0 数据入口（运维给 AI 表：https://alidocs.dingtalk.com/i/nodes/kDnRL6jAJMzYgyx5f9X4roke8yMoPYe1）
- 「线下总经办&省外」表（数据表 hERWDMS）：行=板块（标题主列：线下总经办/谢坚钰、省外/余云涛），列=1日~30日日销 + 日均目标 + 9月销量目标（元）
- 清单注册 `daily_report_offline_zjbsw`（melt 同 vanke 构；标题→responsible_person 板块键、责任人→department 留痕、inject region=线下总经办&省外 → extract 映射 `offline_extra`）；清单已备份 .bak-20260923b
- **李树军不在此表**：按表内 标题 如实呈现（线下总经办/省外）；若李树军另有数据，表内加一行即零代码并入（板块配置驱动）；「线下总经办=李树军口径」待运维一句话确认
- **偏差预警**：表内 17日 单元格 线下总经办 3,322,147 / 省外 1,519,440（单日超过月目标量级，疑为累计值填入单日格）——按单元格语义落库为 9-17 当日销售额，待运维核实

### P1-P3 实现（commits 971b127 / 58eec6d / e264bff / 0d61719，全量 1445 例绿）
- 新模块 `common/daily_robot/offline_summary.py`：4 板块（杭州/绍兴=报数落库，省外/线下总经办=offline_extra 按板块键过滤）+ 线下整体（逐日求和再算，禁止比率加和）
- 口径：日环比=前一自然日、周环比=上周同星期几、月环比=本月1日至当日累计÷上月1日至同日日累计；月目标按人 MAX 再求和、达成率走 `achievement_rate`；**名称含「合计」行一律排除**（AI 表自带 杭州合计/余杭合计 等，不排则区域 SUM 双倍计数——dry-run 实测杭州 1978.6万→711.9万 修正）；pymysql `%` 格式化坑：LIKE 字面量须 `%%合计%%`
- `agg_offline_daily` 新表（迁移 `mart-ops-agg-offline-daily-v1`，(stat_date, scope) 幂等覆盖）；outbox kind +3（`offline_daily/weekly/monthly`，repository + delivery 双白名单）
- 调度：`offline-daily-summary` 20:30 每日 / `offline-weekly-summary` 周一 09:30 / `offline-monthly-summary` 每月1日 10:00（croniter 原生支持），Nacos 已注册 enabled（--if-missing 增量，published=3）
- dry-run 渲染（9-23）：杭州 711.9万/59.9%、绍兴 207.9万/55.1%、省外 151.9万/151.9%、总经办 332.2万/151.4%、整体 1403.9万/74.5% ✅

### Redis 裁决（运维指令「改功能要接入 Redis，不合适给理由」）
- **不接**。理由：① 日/周/月报是低频批量任务（每天至多 3 次触发），无高频重复读取；② agg 表本身即持久化预计算层，叠 Redis=给缓存加缓存；③ 幂等/并发已由 outbox dedupe_key + scheduler MySQL 命名锁覆盖，引入 Redis 第二协调源在主备切换时有重复发群消息风险；④ 汇总须含最后一分钟报数，TTL 陈旧是财务口径事故；⑤ 栈内 Redis 既定用途是 bi-web 查询缓存（fail-open），本功能无 bi-web 面，P4 看板落地时直接走 bi-web 既有缓存层零新增
- 何时该接：群内高频交互查询或 P4 看板直查原始 fact 时，按 bi-web 同款 fail-open 模式再加

### 重要事故根因登记：compose restart 不应用 env 变更（D15）
- sync-dingtalk 连报 `sync_error`，根因链：04:2x 空壳修复把 app.env `PUBLIC_DATA_CONFIG` 改指 `/opt/dops/live/source-manifest.json`，但**容器 env 在 create 时固化，`docker compose restart` 不重读 env_file**——scheduler 容器（00:04 创建）一直是旧的 contract.json 空壳路径，且 `/opt/dops/live` 未挂载
- 修复：compose 给 scheduler + gateway 补 `- /opt/dops/live:/opt/dops/live:ro`（gateway 的 load_settings 也校验该文件存在性，缺挂载曾致其 crash-loop），`compose up -d` **重建**（非 restart）→ env/挂载生效
- 连带收益：今晚 02:00 自动 sync 本会因陈旧 env 复现空壳事故，已提前拆除；**教训：今后改 app.env 必须 `compose up -d` 重建，restart 仅用于纯代码（bind 挂载）变更**
- D16：CLI `migrate` 入口报 migration_error 但直调 `apply_live_migrations` 成功（agg 表已建、版本已登记）——CLI 还连 manual 库/拆库迁移，疑似其中一环 pre-existing 失败，待查（不阻塞，agg 迁移已落）

### 验证
- sync-dingtalk（run 5769f50d）：`daily_report_offline_zjbsw` records_read=2 completed；extract-mart（run 035a212e）completed
- 事实行：`(offline_extra, 线下总经办, 谢坚钰, 9-17, 3322147, 目标2195000)` / `(offline_extra, 省外, 余云涛, 9-17, 1519440, 目标1000000)` ✅
- Nacos 三管线回读 enabled+schedule 正确；scheduler/gateway 重建后 RestartCount 正常、Stream 重连正常
- 今晚 20:30 首条「线下整体日报」将自动投递到线下整体群（dedupe 按日幂等）；周报 9-28（周一）、月报 10-01 首跑

### 17日 大单核实 + 累计格差值管道（16:3x 北京，运维双裁决）
- **证据链**（AI 表编辑历史 API）：总经办行 9-20 16:34 建行填元数据 → 16:37 把 3322147 填进 17日格（建表后 3 分钟一次性回填）；省外行同模式
- **裁决 1**：17日 332万/152万 = **当日真实大单**，维持现状不动
- **裁决 2**：填表人后续只填「累计到当天」→ 管道加差值逻辑（commit `87207d0`）：melt 新增 `value_semantics: "cumulative"`（当日 = 本格 − 上一有值格（日期序），**首格原样 = 期初**；空串/非数字按未填跳过；累计回落输出负值=冲销；daily 缺省逐字不变，vanke/杭绍等存量数据集零影响）；manifest 解析校验该键；测试 +10（乱序 date_columns/隔日差值/千分位/负值/首格保留），全量 1458 例绿
- 上机：清单 zjbsw transform 置 `cumulative`（备份 .bak-20260923c）→ sync（run 4752c984，zjbsw records_read=2 completed）→ extract（837 行重投影）→ **事实行复核：17日 省外 1,519,440 / 总经办 3,322,147 原值保留** ✅
- 踩坑：`docker cp` 撞上只读挂载报 `read-only file system`（live 挂载后新增），改 `cat | docker exec -i sh -c 'cat > ...'` 管道注入
- 待办（运维已提及，未开工）：李树军 = 绍兴区域内独立部门、绍兴播报独立计算 → 线下整体汇总板块拆分（shaoxing 剔除李树军 + lishujun 独立板块，AGG_SCOPES 配置行改动），等 17日事项闭环后启动

### pages-offline_all 板块榜 + 李树军拆分（17:1x 北京，运维追问「offline_all 不是给你要求了吗」+ 李树军补充口径）
- **李树军数据形态实测**：region=shaoxing、responsible_person=李树军、department=线下运营中心（19 行、月累计 14.29万、月目标 103万）——绍兴播报中他本就独立成行
- **AGG_SCOPES 升级锚点模型**（commit `80689dc`）：("person",名)/("dept",部门)/("dept_not",部门)；绍兴板块=shaoxing 剔除线下运营中心，李树军板块=线下运营中心——日/周/月报、agg、页面同口径同步生效（agg 行 5→6）
- **pages-offline_all**：线下整体无 region=offline_all 事实行，mart_cli leaderboard-html 特判 offline_all → `offline_summary.build_offline_all_html`（板块即行复用 build_html 骨架；completed=自然日累计与 20:30 群播报同口径；进度条按工作日与其他页一致；排序同 mart_collect 键）；scheduler+seed 注册（08:30），Nacos --if-missing published=1
- 验证：公网 200（`<title>线下整体销售完成率榜单 · 9月</title>`），李树军行在列（14.3万/103万/13.9%）；leaderboardUrl offline_all 已回填发布（6 regions），gateway restart；**注意今晨 08:30 四区域 pages 管线已自动正常再生**（hangzhou/qudao/shaoxing/vanke 时间戳 08:30 ✅）
- 全量 1461 例绿（+3：拆分断言/HTML 冒烟/排序）；junpin 成唯一无页面区域（无 pages 管线，同法可补）
- 观察：服务器出现 `dops-ntp` 容器（非本链路产出，未触碰，待运维说明）

### offline_all 页重构：人员总榜 + 日/周/月三维度板块（17:3x 北京，运维裁决「要全、没有人例外、日月星三板块」）
- **人员总榜**（commit `e34fad4`）：`OFFLINE_PEOPLE_REGIONS=(hangzhou, shaoxing, offline_extra)` 三区 `mart_collect` 合并——与各区域榜单页逐行同口径（含李树军、省外/总经办责任人），「合计」行由 mart_collect 统一跳过，新增线下区域只改登记元组
- **三维度板块**（extra_panels，qudao_panels 同款机制，单板块失败占位降级）：
  - 日维度：锚定最近有数据自然日（08:30 再生时今日多为空，自动回退昨日），列 当日/日环比/周环比
  - 周维度：本周=周一至今 vs 上周同期
  - 月维度：月累计/月目标/达成率/月环比
- 验证：公网 200（49KB），三板块标题在列，日维度锚定 9-22（杭州 23.9万 +55.8%、绍兴 4.8万、李树军 6,636、整体 29.3万）；页面随 08:30 调度每日再生
- 全量 1461 例绿

### 看板统一 T-1（17:5x 北京，运维裁决「今天的看板，昨天的数据」）
- commit `ef0d977` + `091c071`：三维度板块取数窗口/月目标月份/周月区间/日维度报告日上限全部锚定 business_date−1；人员总榜本就 T-1（mart_collect 不含当日）→ 页面口径全统一；周维度脚注同步改「周一至昨日」
- 验证：日维度锚定 9-22（整体 29.3万）、周维度 9-21~9-22（61.3万）、月维度累计 1403.9万/74.5%；绍兴拆分后 193.6万+李树军 14.3万=207.9万、目标 274.1+103=377.1 与拆分前交叉一致 ✅

### 三维度标签化 + 人/部门归位（18:1x 北京，运维裁决「标签分日月周、内部A链接、人与部门不要混乱」）
- commit `f2d3065`：日/周/月三板块合成**单标签面板**（复用页面 .tabs/.tab 骨架）——tab 就地切换（swDim）、`#dim-daily/#dim-weekly/#dim-monthly` 页内锚点可深链接收藏（点击写 hash、加载按 hash 激活；无 JS 时锚点退化为跳转、三段全见不丢内容）
- **人/部门归位**（运维口径「对部门把握不准看通讯录」）：① offline_extra 行姓名↔部门互换——fact 的 responsible_person 是板块（省外/线下总经办）、department 才是责任人（余云涛/谢坚钰），此前页面人名栏显示板块名属错位；② 杭/绍人员部门以 `dim_robot_member`（通讯录快照）为权威源覆盖，`fetch_member_dept_map(region,name)→dept_name`，无匹配保留表内部门兜底（表内用名≠通讯录实名的别名场景）
- 验证：公网标签锚点在列、`余云涛→省外`、`谢坚钰→线下总经办` 行正确；全量 1461 例绿

### bi-web 上云（阶段 1 内网灰度，19:0x 北京）
- **前置核验全绿**：云上 repo 的 `common/bi_web/{app,config}.py`、`common/public_data/settings.py` 与本地 CRLF 归一化 SHA256 逐字节一致（无需同步代码）；Nacos BI 组实测可达（NacosDashboardSource，l1-cockpit enabled、11 卡，pipeline `bi-web` enabled=true，bi.seed 15 条已在）；app.env 键齐（RDS/Nacos/Redis/三个 seed 路径全指 /opt/dops/repo，ro 挂载即覆盖）；8080/18080 宿主机空闲
- **编排**：`docker/cloud/docker-compose.dops.yml` 加 `bi-web` 服务（repo 真源，云上副本已同步，改动前备份 `.bak-20260923-biweb`）。与 host 网络三件套不同走**桥接 + `18080:8080` 映射**——uvicorn 端口代码内固定 8080（`app.serve`），映射宿主 18080 对齐方案 §2.3（sg-dops-app 18080 已限 VPC 内网）；env 补 `PUBLIC_DATA_SERVICE_ID=bi-web` + `PUBLIC_DATA_BI_SEED`；挂载 repo:ro + live:ro（Settings 启动即读 source-manifest.json）；python urllib 健康检查
- **阶段 1 安全裁决**：免登凭据（BI_WEB_SESSION_SECRET/BI_DINGTALK_APPKEY/APPSECRET）未注入 → 身份层为全开放（auth 设计既定行为），故**不配 nginx 公网 server 块**，访问入口仅 VPC 内网 `http://192.168.0.3:18080`；阶段 2（ICP 备案+域名+免登凭据）再反代并置 `BI_WEB_SESSION_SECURE=1`
- **验证**：容器 healthy；`/healthz` ok+mart 通；`/diagnostics/cache` **backend=redis**（Redis 交付后首个真实消费者）；导航 API 6 看板在列；卡片 `kpi_channel_mtd` 首查 200/178ms（miss）→ 二查 200/4ms（hit），计数器 hits=1/misses=2/errors=0、后端均延 7.5ms；`kpi_annual_progress` 返回真实数（年累计 8,792.7万=线下 5,529.9万+电商 3,262.8万）；**dops-ctrl 内网访问 200 ✓**；**公网 `203.205.91.241:18080` 实测超时（000）✓**（负向验证 SG 拦截）
- 坑登记：`docker cp` 对 scheduler 容器再报 `no such directory`，沿用 `cat | docker exec -i` 管道注入；本地 PowerShell `curl` 是 Invoke-WebRequest 别名，公网负向验证须用 `curl.exe`
- 复用脚本：`tools/check_bi_nacos.py`（容器内 Nacos BI 组 + 门控探针）
- 剩余（阶段 2 前置）：ECS 续费 C0（09-26 前）、regions 真值（G2）、T7 全链路冒烟、免登凭据注入 + 钉钉后台首页地址、域名+ICP 备案
