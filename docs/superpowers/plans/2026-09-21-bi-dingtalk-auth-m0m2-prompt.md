# BI 钉钉免登 × 权限管理 · M0–M2 派发提示词（2026-09-21）

> 用途：启动阶段 C 首批——钉钉免登认证骨架（M1）+ grant/audit 建表与 admin 自举（M0）+ 页面级权限与 ops-web 权限管理（M2）。
>
> - **§0** = 决策背景（已定稿，执行 agent 无需再争）
> - **§A** = 可复制执行提示词（本批唯一要派发的内容）
> - **§B** = 明确不动（本批）
> - **§C** = 本批之后的阻塞与归属（M3/M4）
> - **§D** = 事实锚点表
> - **§E** = 一句话总结

---

## §0 决策背景（已定稿）

| # | 决策 | 结论 |
|---|---|---|
| 1 | 认证 | 钉钉免登（authCode 换 userid）+ HMAC 签名 cookie（8h 滑动）；Bearer token 保留兜底 |
| 2 | 授权 | **deny-by-default**：grant 表无记录 = 只见「未授权」提示页；首发布仅 admin 一人 |
| 3 | admin | **王城**（信息技术经理，总经办），userid `014341566058939427`（2026-09-21 经通讯录 API 确认） |
| 4 | 操作面 | 独立服务 **ops-web**（权限管理为其第一个功能），grant 数据写 `mart_ops` **不写 Nacos** |
| 5 | bi-web 外呼 | 合约从「无外呼」放宽为「白名单 = api.dingtalk.com / oapi.dingtalk.com」（方案 A，放弃 sidecar） |
| 6 | 权限故障语义 | **fail-closed**：权限数据读不到 → 仅提示页/403，绝不放大可见面（配置链其余部分仍 fail-open） |
| 7 | 组织同步 | `org.seed.json` 已加 `hq: [1050143465]`（总部子树，含总经办/财务部），随日常 sync-dingtalk 落 `dim_robot_member` |

设计全文：`docs/superpowers/specs/2026-09-21-bi-dingtalk-auth-design.md`（评审定稿 v3），本提示词与其冲突时**以设计稿为准**。

---

## §A 可复制执行提示词（M0–M2）

> 复制以下整段给执行 agent。

````text
你是 auth-agent。任务：落地 BI 看板钉钉免登与权限管理的首批（设计稿 M0+M1+M2），
产出 bi-web 认证/授权骨架 + ops-web 权限管理服务。本批不做行级权限（M3）与前端体验（M4）。

【契约来源，先读】
1. docs/superpowers/specs/2026-09-21-bi-dingtalk-auth-design.md（评审定稿 v3，唯一口径真源）
2. docs/superpowers/specs/2026-09-12-bi-web-design.md §8 认证与暴露、§10 合约测试约定
3. docs/BI建设指南.md §2.2 核心架构原则（SQL 在代码、bi-web 只读 mart、fail-open 链）
4. common/bi_web/app.py（现有 Bearer 依赖与请求守卫结构）
5. common/public_data/org_read.py（钉钉通讯录只读网关，token 获取方式复用它的模式）

【范围边界】
只改/新增：
  - common/bi_web/ 下新增 auth.py（session 签发/校验）、authz.py（Viewer 解析 + 准入门）；
    app.py 仅在依赖装配处接线
  - common/ops_web/（新建包：ops-web 的 grant 管理 API + 极简管理页，Jinja2 同 bi-web 风格）
  - common/public_data/ 下新增 bi_authz 迁移（grant + audit 两表 DDL，随 live_migrations 同款模式）
  - docker/integration/ 新增 ops.seed.yaml（首批 admin seed）+ docker-compose.integration.yml
    加 ops-web 服务（独立 profile，端口仅回环 127.0.0.1:181xx）
  - tests/common/ 新增离线单测；tests/integration/ 新增/修订合约测试
不改（详见 §B）：
  - queries.py / cards.py 任何卡片 SQL 与注册表签名（行级注入是 M3）
  - sync-dingtalk / extract-mart 管线逻辑；org.seed.json（本批前已改好，勿动）
  - frontend/**；机器人链路；Nacos 既有 dataId 结构（只新增 required_scope 可选字段的消费）
  - 凭据文件与本机 credentials/ 目录

【先确认的三件事，动手前必须回答】
Q1 合约测试现状：找到断言「bi-web 无外呼/无凭据挂载」的既有测试（tests/integration/ 下，
   大概率在 test_integration_environment.py），列出具体断言行号——本批要改的就是它们，
   改白名单语义而不是删除。
Q2 Nacos 看板定义消费链：config.py 的 dashboard 模型加 required_scope（可选字段，缺省
   None = 已准入即可见）时，fail-open 链（Nacos → seed → 默认）三层是否都兼容旧格式？
   旧格式看板（无该字段）必须零影响。
Q3 dim_robot_member 在测试库已有 49 行快照（mart_ops_test），但总部子树尚未同步——
   在职校验的单测不得依赖真实库，一律 FakeConnection/fixture；admin 王城不在快照里
   属预期，自举 seed 的逻辑是「grant 表有 admin 记录即准入」，不得以 dim_robot_member
   无此人而拒绝 admin。

【铁律（违反即返工）】
1. deny-by-default：bi_authz_grant 无任何记录的用户，认证通过也只见「未授权」提示页。
   admin（grant_type='admin'）恒准入。首发布 seed 仅 admin 一条记录。
2. fail-closed 权限语义：grant 表读取失败、required_scope 声明损坏、Viewer 解析异常——
   一律按「仅未授权提示页 / 403」处理，绝不退化为全员可见。此语义必须有独立单测钉死。
3. bi-web 只读不破：bi-web 对 bi_authz_grant 只 SELECT；写路径（INSERT/DELETE + audit）
   只存在于 ops-web。合约测试断言两服务的数据库权限面。
4. SQL 在代码：Viewer 的行级集合本批只算好（allowed_regions），不注入任何卡片查询——
   queries.py 零 diff。页面级判定在路由/导航层做。
5. session 纯签名 cookie（stdlib hmac/hashlib/secrets），密钥 BI_WEB_SESSION_SECRET
   环境变量注入，不设服务端存储，不引新依赖。钉钉 API 调用走 httpx（已在依赖），
   token/jsapi_ticket 用 TTL 缓存（复用 app.py 里 _TTLGate 同款模式）。
6. 凭据零入库零入仓：BI_DINGTALK_APPKEY/APPSECRET 仓库外挂载；错误响应泛化（不含
   URL、请求体、响应体、AppSecret），与 org_read.py 的错误纪律一致；userid 在日志中脱敏。
7. 两服务共用认证代码：session/免登交换实现放 common/bi_web/auth.py（或 common/auth/），
   ops-web import 复用，禁止复制一份改改用。
8. ops-web 访问控制：网络层仅回环绑定 + 应用层 admin scope 校验双闸；它不是面向全员的服务。

【分步任务】
T0 · 基线
  - 跑 `python -m unittest discover -s tests -t .` 记录测试基线数字。
  - 记录 bi-web 当前 /healthz 与 /d/l1-cockpit 在 Bearer 下的行为作为回归基准。

T1 · M0 建表与自举
  - bi_authz_grant / bi_authz_grant_audit 两表 DDL（设计稿 §4.2 的 SQL 原样采用），
    迁移模式对齐 common/public_data/live_migrations.py 的既有风格。
  - ops.seed.yaml：admin 单条记录——user_id=014341566058939427, grant_type='admin',
    grant_key='-', note='自举管理员 王城'。发布命令与 bi.seed.yaml 同款
    （if_missing=False 语义）。
  - 离线单测：DDL 幂等（连跑两次不炸）、seed 解析校验（缺字段/非法 grant_type 抛错）。

T2 · M1 认证骨架（common/bi_web/auth.py）
  - HMAC 签名 session：签发（userid|exp|nonce + hmac）、校验（过期/篡改/格式错各自
    分支）、8h 滑动续期。纯函数优先，无 IO 部分全部可离线单测。
  - 钉钉交换客户端：authCode → gettoken（TTL 缓存）→ topapi/v2/user/getuserinfo →
    userid；3s 超时 + 1 次重试，失败抛泛化 AuthError。HTTP 层依赖注入（同
    OrgReadGateway 的 request_json 约定），测试不联网。
  - 路由：POST /auth/dingtalk、GET /auth/entry（未授权/未登录提示页，Jinja2）、
    GET /auth/logout。app.py 装配：认证依赖 = session 优先、BI_WEB_TOKEN Bearer 兜底，
    两者都失败 → 302 /auth/entry（API 通道则 401 JSON，保持机器可读）。
  - Bearer 路径零行为变化（T0 基准自证）。

T3 · M2a 授权骨架（common/bi_web/authz.py）
  - resolve_viewer(request) → Viewer(userid, name, scopes, allowed_regions, is_admin)，
    grant 读 mart_ops（只读），TTL 缓存 60s，读失败 → fail-closed（铁律 2）。
  - 准入门：无 grant 记录 → /auth/entry 提示页（deny-by-default）；admin 恒过。
  - 页面级：config.py dashboard 模型加可选 required_scope；路由守卫 + 导航过滤；
    scope 不匹配 → 403 JSON（API）/ 提示页（页面）。旧看板定义（无该字段）零影响（Q2）。
  - 离线单测：deny-by-default、admin 恒准入、fail-closed 三态（表读失败/声明损坏/
    解析异常）、required_scope 匹配与不匹配、旧格式看板兼容。

T4 · M2b ops-web 权限管理（common/ops_web/ + compose）
  - 页面：成员列表（读 mart_ops_test.dim_robot_member 同源，按 region/dept 分组）、
    单人授权表单（scope/region 勾选）、grant 现状表、audit 流水页。Jinja2 极简风，
    不引前端框架。
  - API：grant/revoke 各一个端点，写 bi_authz_grant + 同行落 audit（actor 来自
    session，事务内两写）。所有写端点仅 admin scope 可访问（双闸：应用层校验 +
    compose 仅回环）。
  - 批量模板：按 region 一键开通（region grant 批量 upsert），候选名单读
    dim_robot_member——批量操作也必须逐行落 audit。
  - compose：ops-web 独立 profile（profiles: [ops-web]），127.0.0.1:18100:8080，
    数据库环境变量只给 mart；凭据变量仅 BI_DINGTALK_APPKEY/APPSECRET（仓库外挂载）。
  - 合约测试修订（Q1 定位处）：bi-web 与 ops-web 的外呼白名单 =
    {api.dingtalk.com, oapi.dingtalk.com}；bi-web 无写库凭据面；ops-web 端口仅回环。

T5 · 集成冒烟
  - `docker compose --profile bi-web --profile ops-web up` 后：
    无 session 无 token → /d/l1-cockpit 跳 /auth/entry；Bearer 旧 token → 正常；
    grant 表手工插一条测试 region 记录 → 该用户可见、其余仍提示页（可用 SQL 直插
    模拟，注明这是测试手段不是功能）。结果截图/日志写进 plan。

【验收 · 完成定义】
1. 新增单测全绿 + 既有测试零回归（对比 T0 基线数字）。
2. queries.py / cards.py / org.seed.json / frontend/** 零 diff（用 diff 自证）。
3. 铁律 1/2/3 各有独立断言：deny-by-default、fail-closed、bi-web 无写路径。
4. Bearer 兜底行为与 T0 基准一致；旧格式看板（无 required_scope）行为不变。
5. compose 合约测试改白名单后全绿；ops-web 仅回环可断言。
6. T5 冒烟三态（未登录 / Bearer / 已授权用户）各有证据。

【汇报要求】
新建 docs/superpowers/plans/2026-09-21-bi-dingtalk-auth-m0m2-plan.md，至少记录：
  - T0 基线数字与最终数字；
  - Q1 合约测试的改动点（原断言行号 → 新语义）；
  - Q2 旧格式看板兼容的实现方式；
  - 钉钉交换客户端的实际端点与字段（以官方文档现行形态为准，偏差要登记）；
  - ops-web 端口与 profile 名（若与提示词不同要说明）；
  - 留待 M3/M4 的钩子清单（allowed_regions 已算未用、/auth/jsapi-config 未做等）。
若发现设计稿与代码事实不符：以代码为准，在 plan 里登记偏差，不要为了让文档好看
去改背后的语义。
````

---

## §B 明确不动（本批）

| 对象 | 为什么不动 |
|---|---|
| `queries.py` / `cards.py` | 行级谓词注入是 M3，本批卡片查询零 diff——Viewer 的 allowed_regions 算好但不用 |
| `org.seed.json` | 本批前已加 `hq`，执行 agent 勿再动；同步生效归日常跑批 |
| sync-dingtalk / extract-mart | 组织同步管线逻辑不变，只是种子多了一个 region |
| `frontend/bi-react/**` | 前端免登体验是 M4 |
| Nacos 既有 dataId 结构 | 只新增可选字段 `required_scope` 的消费，旧看板零迁移 |
| 大屏 token 白名单位置 | 开放点 #4，本批维持 Bearer 兜底现状即可 |

---

## §C 本批之后的阻塞与归属

| # | 事项 | 归属 |
|---|---|---|
| C1 | 行级权限：`allowed_regions` 注入查询层 + 逐卡片合约单测 | M3（等 B1 区域口径固化后做，避免重复改卡） |
| C2 | 前端体验：钉钉容器检测、免登跳转链、PC 扫码、`/auth/jsapi-config` | M4 |
| C3 | HTTPS 反代 + 域名证书上部署 | M4 部署侧，需王城提供域名 |
| C4 | 总部子树同步进 `dim_robot_member`（王城在册） | 日常 sync-dingtalk 跑批自动生效，无需立项 |
| C5 | ops-web 后续功能（seed 发布 UI、阈值编辑、整站开关、sync_runs 审计视图） | 权限管理上线后按运维痛点排，开放点 #5 |

---

## §D 事实锚点表

| 对象 | 位置 |
|---|---|
| 设计真源（定稿 v3） | `docs/superpowers/specs/2026-09-21-bi-dingtalk-auth-design.md` |
| 现有 Bearer 依赖 | `common/bi_web/app.py` `_build_bearer_dependency`（约 :383） |
| 请求守卫结构说明 | `common/bi_web/app.py:57-62`（模块 docstring） |
| TTL 缓存模式范本 | `common/bi_web/app.py` `_TTLGate` |
| 钉钉通讯录网关（token/错误纪律范本） | `common/public_data/org_read.py` `OrgReadGateway` |
| grant 身份真源 | `mart_ops.dim_robot_member`（DDL：`common/public_data/mart_extract_schema.py:352`） |
| 迁移风格范本 | `common/public_data/live_migrations.py` |
| 看板配置模型 | `common/bi_web/config.py`（fail-open 链：Nacos → seed → 默认） |
| bi-web compose 约定 | `docker-compose.integration.yml` `bi-web:`（profiles/回环端口/零凭据注释约 :514-517） |
| 组织种子（已含 hq） | `docker/integration/org.seed.json` |
| admin 身份 | 王城，userid `014341566058939427`（总经办，信息技术经理） |
| 凭据（仓库外） | `e:/repos/credentials/source-credentials.json` → `dingtalk.app_key/app_secret` |
| 测试库快照 | `mart_ops_test.dim_robot_member`（49 行，无总部成员属预期） |

---

## §E 一句话总结

**本批干三件事**：给 bi-web 装上钉钉免登 + deny-by-default 准入门，建 grant/audit 两表并自举唯一 admin（王城），再交付 ops-web 权限管理让王城从此自助分配——卡片 SQL 一行不动，行级和前端体验留给 M3/M4。
