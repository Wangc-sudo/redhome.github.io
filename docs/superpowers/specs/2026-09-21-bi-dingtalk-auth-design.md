# BI 看板 × 钉钉 权限深度集成设计方案

状态：评审定稿（2026-09-21；运维系统 ops-web 形态并入）
关联：`2026-09-12-bi-web-design.md` §8（阶段 C 既定方向）、`docs/BI建设指南.md` §5 阶段 C 行、`docs/E类确认-业务单元与负责人清单-2026-09-21.md`

---

## 1. 目标与范围

把 bi-web 的认证从 `BI_WEB_TOKEN` Bearer 升级为**钉钉免登**，并在 BI 侧建立授权模型，实现：

1. **认证（钉钉负责）**：用户从钉钉工作台点开看板，免登直进；后端用 authCode 换 `userid`，签发会话。
2. **授权（BI 自建）**：`userid → 页面可见性 + 行级数据范围`，注入卡片查询层。
3. **降级兼容**：Bearer token 保留为大屏/审计/过渡通道；非钉钉环境（PC 浏览器）走钉钉扫码登录。
4. **运维系统 ops-web**：权限的**操作面**独立成运维系统，权限管理为其第一个功能；后续收口 seed 发布/回滚、阈值编辑、整站开关、采集审计查看。

非目标：应用市场上架、跨企业分发、钉钉侧审批流集成。

## 2. 核心结论：组件引入评估

**技术栈零新增；部署单元 +1**——运维系统 `ops-web`（FastAPI 轻服务，复用 bi-web 骨架与免登模块，网络层仅 IT 可达）。其余能力全部在现有栈内：

| 能力 | 依赖 | 现状 |
|---|---|---|
| 钉钉免登 HTTP API（gettoken / getuserinfo） | `httpx`（同步阻塞式即可） | ✅ 已在 requirements |
| 会话签发/校验（HMAC 签名 cookie） | stdlib `hmac`/`hashlib`/`secrets`；密钥 `BI_WEB_SESSION_SECRET` 环境变量 | ✅ 零新依赖 |
| JSAPI 签名（jsapi_ticket 缓存） | `httpx` + 现有 `_TTLGate` 同款 TTL 缓存模式 | ✅ 模式现成 |
| 身份与行级映射 | `mart_ops.dim_robot_member`（user_id/name/**region**/dept_id/is_active，已从钉钉组织同步） | ✅ 已在产，设计稿 §8 点名复用 |
| 页面级权限编排 | Nacos `group=BI`（看板定义加 `required_scope` 字段，fail-open 链不变） | ✅ 通道现成 |
| grant 数据操作面 | `ops-web` 写 `mart_ops.bi_authz_grant` + 审计日志（**不写 Nacos**——grant 是频繁变更的业务数据，需审计，Nacos 只放声明性编排） | ➕ 唯一新增部署单元 |
| 前端钉钉容器内免登 | `dingtalk-jsapi`（bi-react 唯一新增 npm 依赖） | ➕ 前端一个包 |
| HTTPS 公网入口 | nginx/Caddy 反代 + 证书（**部署侧，非软件组件**） | ➕ compose 加一个反代服务或云网关 |

钉钉侧也**无资质卡点**：复用或新建一个企业内部应用（H5 微应用），管理员后台勾「通讯录只读」权限点即可；免登/扫码/JSAPI 都是企业内部应用标准能力，无审核无上架。

## 3. 总体架构

```
钉钉工作台 / 钉钉内浏览器                PC 浏览器（非钉钉）
        │ 免登                                │ 扫码 OAuth
        ▼                                    ▼
┌──────────────────────────────────────────────────────┐
│ HTTPS 反代（新部署单元：域名+证书+ws 转发）            │
└──────────────────────────────────────────────────────┘
        ▼
bi-web（FastAPI，面向全员，只读）
  ├─ /auth/dingtalk   POST {authCode} → 调 api.dingtalk.com 换 userid
  │                     → HMAC session cookie（HttpOnly, 8h 滑动）
  ├─ /auth/qrcode/*   扫码登录回调 → 同上签发 session
  ├─ /auth/jsapi-config  JSAPI 签名（ticket TTL 缓存）
  ├─ 认证依赖：session 优先，Bearer 兜底（大屏/审计）
  └─ 授权依赖：resolve_viewer(request) → Viewer(userid, name, region, scopes)
        ▼
卡片查询层（queries.py / cards.py）：Viewer 上下文注入
  ├─ 页面级：dashboard 定义 required_scope ⊆ viewer.scopes，否则 403/导航隐藏
  └─ 行级：WHERE region IN viewer.allowed_regions（参数化，SQL 仍在代码）
        ▼ 只读
mart_ops：dim_robot_member（身份+region 真源） + bi_authz_grant + bi_authz_grant_audit
        ▲ 写（唯一写入方）
ops-web（FastAPI，仅 IT 可达：回环/内网 ACL）
  ├─ 功能1 权限管理：grant 增删改 + 审计日志（写 bi_authz_grant*）
  ├─ 功能2（后续）：seed 发布/回滚、阈值编辑、整站开关（写 Nacos）
  └─ 功能3（后续）：sync_runs 采集审计只读视图
  认证：与 bi-web 共用免登/session 代码包（common/），admin scope 白名单自举
```

**职责分工一句话**：钉钉回答「你是谁」，bi-web 回答「你能看哪页哪行」，ops-web 回答「谁能改这个答案」；映射数据落在已有的 `dim_robot_member` + grant/audit 两张新表。

## 4. 授权模型

### 4.1 三层权限 + 准入门

**准入语义：deny-by-default（2026-09-21 定稿 v3）**——`bi_authz_grant` 中无任何记录的用户，认证通过但**准入拒绝**：只见「未授权，请联系管理员开通」提示页，无任何页面可见。首发布 grant 表仅 admin（王城）一条记录，其余人员授权全部由 admin 在 ops-web 统一分配。ops-web 提供批量授权模板（如「按 region 一键开通本区域可见」），候选名单直接读 `dim_robot_member`——区域归属是**授权时的输入**，不是自动生效的权限。

| 层 | 控制什么 | 数据源 | 规则 |
|---|---|---|---|
| 准入 | 能否看到任何页面 | `bi_authz_grant` 存在任一记录 | 无记录 → 「未授权」提示页；admin 恒准入 |
| 页面级 | dashboard 可见性（导航隐藏 + 服务端 403） | Nacos 看板定义新增 `required_scope`（如 `fin`、`ecom`） | 无声明 = 已准入即可见；有声明 = 需对应 scope grant |
| 行级 | 区域内数据可见（`fact_daily_report_offline.region` 等） | `bi_authz_grant` 的 `region` grant | 显式授予的 region 集合，**无自动默认** |
| 管理级 | ops-web 访问、配置发布、大屏模式、全量可见 | `bi_authz_grant` 的 `admin` scope | 仅 admin（王城） |

### 4.2 新表 `bi_authz_grant` + `bi_authz_grant_audit`（ops-web 维护）

```sql
CREATE TABLE `bi_authz_grant` (
  `user_id`    VARCHAR(64)  NOT NULL,
  `grant_type` VARCHAR(16)  NOT NULL,   -- 'scope' | 'region' | 'admin'
  `grant_key`  VARCHAR(64)  NOT NULL,   -- scope 名 / region 值 / '-'
  `note`       VARCHAR(255) DEFAULT NULL,
  `updated_by` VARCHAR(64)  DEFAULT NULL,  -- 最后操作人 userid（ops-web 写入）
  `updated_at` DATETIME(6)  DEFAULT NULL,
  PRIMARY KEY (`user_id`, `grant_type`, `grant_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `bi_authz_grant_audit` (
  `id`         BIGINT AUTO_INCREMENT PRIMARY KEY,
  `actor`      VARCHAR(64)  NOT NULL,   -- 操作人 userid
  `action`     VARCHAR(16)  NOT NULL,   -- 'grant' | 'revoke'
  `user_id`    VARCHAR(64)  NOT NULL,
  `grant_type` VARCHAR(16)  NOT NULL,
  `grant_key`  VARCHAR(64)  NOT NULL,
  `note`       VARCHAR(255) DEFAULT NULL,
  `created_at` DATETIME(6)  NOT NULL,
  KEY `idx_user` (`user_id`),
  KEY `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- **维护方式（2026-09-21 定稿 v2）**：单 admin 模型——项目负责人本人即唯一管理员。seed 仅用于**自举**：admin 本人 userid 一次性落库；其余所有人员的例外授权由 admin 在 **ops-web 权限管理功能**自助分配，每次变更落一行 audit（谁、何时、授予/收回、什么范围）。无首批映射、无需业务负责人确认环节。
- 与 `dim_target` 不同：grant 是运维数据不是业务口径，**不走 seed 版本受控**（自举批除外）；Nacos 也不放 grant——它是配置中心不是数据库，审计弱、无行级结构。
- 总经理/财务（资金安全页白名单）、大区总（多 region）都落这张表，不改组织架构。
- `is_active=0`（离职）自动失效：`resolve_viewer` 先查 `dim_robot_member`，在职才继续。

### 4.3 Viewer 解析（每请求，TTL 缓存 60s）

```python
Viewer(userid, name, scopes: frozenset, allowed_regions: frozenset)
# scopes          = {g.grant_key for g in grants if type='scope'} ∪ ({'fin'} if admin)
# allowed_regions = {g.grant_key for g in grants if type='region'}   # 无自动默认，全部显式授予
```

## 5. 认证流

### 5.1 钉钉容器内免登（主路径）

1. 前端检测到钉钉 UA → `dingtalk-jsapi` 取 `authCode`；
2. `POST /auth/dingtalk {authCode}` → 后端 `gettoken`（AppKey/Secret，token TTL 缓存）→ `topapi/v2/user/getuserinfo` 换 `userid`；
3. 查 `dim_robot_member`：在职 → 签发 `session = base64(userid|exp|nonce).hmac`，`Set-Cookie: HttpOnly; SameSite=Lax; Secure; Max-Age=8h`；
4. 后续请求认证依赖统一为：`session 有效 → Viewer`；否则 `Bearer` 兜底；都失败 → 401 跳转 `/auth/entry`。

### 5.2 PC 扫码（次路径）

钉钉扫码 OAuth 回调 → 同样换 userid 签 session。复用同一套 session 与 Viewer 解析。

### 5.3 JSAPI 签名

`/auth/jsapi-config?url=...`：缓存 `jsapi_ticket`（TTL ~7000s），按钉钉算法（noncestr/timestamp/url）SHA1 签名返回 agentId/corpId/signature。签名逻辑版本受控，与卡片同标准。

## 6. 权限谓词注入（不破坏「SQL 在代码」）

- `cards.py` 注册表签名扩展：`run(conn, params, viewer)`——`viewer` 由依赖注入，**不允许卡片绕过**；
- `queries.py` 中带区域语义的查询显式接收 `allowed_regions: tuple`，拼参数化 `IN` 子句（`%s` 占位，绝不字符串插值）；
- 无区域语义的卡片（年度目标、资金汇总）声明 `row_scope=None` 豁免；
- 合约单测：逐卡片断言「带 region 的查询必消费 viewer.allowed_regions」，防止新增卡片漏网。

## 7. 架构原则冲突与处置（必须评审）

现行合约测试断言 bi-web **「无外呼」**（compose 无凭据挂载、无 `--live-*`、无外呼）。免登要求 bi-web 调用 `api.dingtalk.com`。

| 方案 | 做法 | 评价 |
|---|---|---|
| **A（推荐）** | 合约放宽为「外呼白名单 = api.dingtalk.com」，凭据 `BI_DINGTALK_APPKEY/APPSECRET` 仓库外挂载，compose 断言凭据变量来源 | 改动最小；bi-web 仍零写库、只读 mart，原则损失可控 |
| B | auth sidecar：免登交换放 dingtalk-gateway，bi-web 只验签 | 边界最干净，但多一跳、多一个部署单元，阶段 C 性价比低 |

取 A，合约测试改写为白名单断言 + 凭据挂载路径断言，错误响应沿用泛化规范（不外泄 AppSecret、userid 仅在审计日志脱敏出现）。

另两条随 ops-web 定稿的原则（2026-09-21）：

1. **bi-web 只读不破**：ops-web 是唯一写 `bi_authz_grant*` 与发 Nacos 的服务，独立部署、网络层仅 IT 可达；bi-web 保持只读 mart + 白名单外呼。两服务共用 `common/` 下的认证/session 代码包，不复制实现。
2. **权限链路 fail-closed 例外**：现有配置链 fail-open（Nacos → seed → 默认）只适用于页面可用性；权限判定必须反向——grant 表或 `required_scope` 声明读取失败时，降级为**仅 L1 首页可见**（资金安全等受控页直接 403），绝不因配置故障放大可见面。该语义入合约单测锁定。

## 8. 安全与降级

- session 密钥 `BI_WEB_SESSION_SECRET` 仓库外注入，泄漏即轮换（全部会话失效，可接受）；
- 大屏模式：专用 `BI_WEB_TOKEN` 大屏 token + `required_scope` 白名单只读页，不发 session；
- 钉钉不可用：Bearer 兜底仍在；`api.dingtalk.com` 调用设 3s 超时 + 1 次重试，失败 503 泛化文案；
- 关停语义不变：Nacos 请求级门控优先于认证（503 全站）；
- 未授权用户（无 grant 记录）：登录成功但只见「未授权，请联系管理员开通」提示页（deny-by-default，见 §4.1）；钉钉有号但 `dim_robot_member` 无记录者同样处理。

## 9. 里程碑与估算

| 里程碑 | 内容 | 估算 |
|---|---|---|
| M0 自举 | `bi_authz_grant`(+audit) 建表迁移 + 唯一 admin seed 落库（**已定：王城，信息技术经理，userid `014341566058939427`**）；总部子树已加入 `org.seed.json`（region `hq`），admin 随日常同步进 `dim_robot_member` | 0.5 人天 |
| M1 认证骨架 | `/auth/dingtalk` + session cookie + Viewer 解析 + Bearer 兜底共存；合约测试改白名单；认证/session 代码独立成 `common/` 共享包 | 3–4 人天 |
| M2 页面级权限 + ops-web 权限管理 | Nacos `required_scope` + 导航过滤 + 403 语义；ops-web 骨架（compose profile、仅 IT 可达）+ grant 增删改 UI + 审计日志；fail-closed 语义入合约 | 5–6 人天 |
| M3 行级权限 | `viewer.allowed_regions` 注入查询层 + 逐卡片合约单测 | 3–4 人天（随 B1 卡片增加回归成本） |
| M4 体验收口 | bi-react 钉钉容器检测 + 免登跳转链 + PC 扫码 + `/auth/jsapi-config`；HTTPS 反代上部署 | 2–3 人天 + 部署半天 |

**前置依赖**（非技术）：admin 本人钉钉 userid（已定：王城 `014341566058939427`）；HTTPS 域名与证书。~~E 类负责人清单~~ **不再是权限前置**——准入语义 deny-by-default：首发布仅 admin 一人可用，其余人员授权全部由 admin 在 ops-web 统一分配（提供按 region 批量开通模板，候选名单读 `dim_robot_member`）。

**建议排期**：与 B1 并行启动 M0+M1（互不阻塞）；M2 中 ops-web 与 bi-web 页面级可并行两线；M3 等 B1 区域口径固化后做，避免重复改卡。ops-web 权限管理上线后，E 类后续调整不再走 seed，由 IT 直接操作。

## 10. 开放点

1. session 存服务端（mart_ops 表/Redis）还是纯签名 cookie？——倾向纯签名（零写库原则不破），登出需求出现再议。
2. 扫码登录用钉钉新版统一授权页还是旧版 sns？——实施时以官方文档现行推荐为准。
3. 区域颗粒度到 `region`（杭/绍）还是下钻到渠道/店铺？——首版到 region，下钻依赖 E 类渠道字典。
4. 大屏 token 的页面白名单放 Nacos 还是 compose 环境变量？——倾向 Nacos（与整站关停同通道）。
5. ops-web 后续功能排序：seed 发布/回滚 UI、阈值编辑、整站开关、sync_runs 审计视图——权限管理上线后按运维痛点排，每个功能独立小里程碑。
