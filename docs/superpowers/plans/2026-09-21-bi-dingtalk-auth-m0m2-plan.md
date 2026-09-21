# BI 钉钉免登 × 权限管理 · M0–M2 执行汇报（2026-09-21）

派发提示词：`docs/superpowers/plans/2026-09-21-bi-dingtalk-auth-m0m2-prompt.md`（§A 整段执行）
设计真源：`docs/superpowers/specs/2026-09-21-bi-dingtalk-auth-design.md`（评审定稿 v3）

---

## 1. 测试基线对比（T0 → 最终）

| 时点 | 数字 | 说明 |
|---|---|---|
| T0 基线 | Ran **1200**，FAILED (failures=1, skipped=39) | 唯一失败为预存问题：`org.seed.json` 加 `hq`（派发文件 §0 决策 7，本批前置）后，`test_public_data_org_read.py:167` 的发版种子断言未跟上 |
| T0 修正后 | 1200 全绿（skipped=39） | 修正该断言为 `[hangzhou, shaoxing, vanke, hq, other]` 并补 hq 部门 ID 断言——属前置决策的连带断言，非本批功能改动 |
| **最终** | Ran **1294**，**OK (skipped=39)** | **+94 个新用例，既有用例零回归** |

T0 Bearer 行为基准（`_build_app(token='t')`，TestClient 离线）与最终复验**逐点一致**：

| 请求 | T0 | 最终 |
|---|---|---|
| `GET /healthz` 无头 | 200 | 200 |
| `GET /d/l1-cockpit` 无头 | 401 | 401 |
| `GET /d/l1-cockpit` Bearer 错 | 401 | 401 |
| `GET /d/l1-cockpit` Bearer 对 | 200 | 200 |
| `GET /api/v1/dashboards` 无头 | 401 | 401 |
| `GET /api/v1/dashboards` Bearer 对 | 200 | 200 |

## 2. Q1 合约测试改动点（原断言行号 → 新语义）

文件 `tests/test_integration_environment.py`：

| 原断言 | 处置 |
|---|---|
| `test_bi_web_is_opt_in_and_read_only` :379 零 volumes | **保留不变**——免登凭据走环境变量透传而非文件挂载，零挂载语义不破 |
| :385 命令无 `--live-*` | 保留不变 |
| :389-392 回环端口 18080 | 保留不变 |
| 原「无外呼/无凭据」整体语义 | **放宽为白名单（设计稿 §7 方案 A）**：新增 `test_bi_web_dingtalk_credentials_are_passthrough_only`——外呼面收敛为 `api.dingtalk.com`/`oapi.dingtalk.com` 所需的恰好三个变量（`BI_WEB_SESSION_SECRET`/`BI_DINGTALK_APPKEY`/`BI_DINGTALK_APPSECRET`），全部 `${...:-}` 宿主机透传、本地解析为空（断言「凭据零烘焙」，不再断言「凭据不存在」） |
| （新增）ops-web 双闸 | `test_ops_web_is_opt_in_loopback_only_and_passthrough`：独立 profile、仅回环 `127.0.0.1:18100:8080`、零挂载、同款凭据透传白名单 |

## 3. Q2 旧格式看板兼容的实现方式

`common/bi_web/config.py`：`parse_dashboard_config` 增加可选字段 `required_scope`（非空字符串或缺省 `None`），`DashboardConfig` 加同名字段（缺省 `None`）。三层 fail-open 链（Nacos → seed → 默认）共用这同一个解析器，旧格式条目（无该字段）在三层都解析为 `None` = 「已准入即可见」，**零迁移、零行为变化**。页面级判定是纯函数 `authz.scope_allows`：`None` 放行任何已准入 viewer；有声明需同名 scope（admin 恒过）。旧格式兼容由 `PageLevelScopeTests.test_legacy_dashboard_without_the_field_is_unaffected` 钉死，config 既有测试全绿。

## 4. Q3 admin 自举与在职校验的次序

`authz.build_viewer`：`('admin', '-')` grant 命中即准入（恒过），**不查 `dim_robot_member`**——自举不以总部子树同步为前提（测试库 49 行快照无王城属预期）。在职校验（`is_active=0` 失效、不在册视同未授权）只约束**非 admin** 用户。钉死用例：`test_admin_grant_admits_without_member_record` / `test_inactive_member_is_denied_even_with_grants` / `test_member_not_on_record_is_denied`。

## 5. 钉钉交换客户端的实际端点与字段

与设计稿 §5.1 一致、与 `org_read.py` 已实测形态同源（`common/bi_web/auth.py` `DingTalkAuthClient`）：

| 步骤 | 端点 | 请求 | 取字段 |
|---|---|---|---|
| gettoken（TTL 缓存 100min） | `POST https://api.dingtalk.com/v1.0/oauth2/accessToken` | `{appKey, appSecret}` | `accessToken` |
| getuserinfo | `POST https://oapi.dingtalk.com/topapi/v2/user/getuserinfo?access_token=…` | `{code: authCode}` | `result.userid`（`errcode==0` 闸门） |

纪律：3s 超时 + 1 次重试；一切失败抛泛化 `AuthError`（不含 URL/请求体/响应体/AppSecret）；userid 日志脱敏（`mask_userid` 只留首尾各两位）。**端点与设计稿无偏差**。

## 6. ops-web 端口与 profile

与提示词一致：profile `ops-web`，端口 `127.0.0.1:18100:8080`，命令 `python3 -m common.ops_web.app`。自举 seed 发布命令（`load-target` 同款写闸）：

```
python -m common.public_data.cli load-ops-seed \
  --seed docker/integration/ops.seed.yaml --confirm-local-test-write
# --if-missing 可切换为「已存在跳过」（默认 if_missing=False 覆盖语义，与 publish-bi 同款）
```

## 7. T5 冒烟三态证据（离线 TestClient，真实 ViewerResolver 链路）

```
态1  无session无token  /d/l1-cockpit -> 302 /auth/entry?reason=login
态1b API 无凭证 -> 401 {'detail': 'unauthorized'}
态2  Bearer旧token /d/l1-cockpit -> 200
态3a 已授权用户(u-zhang, region=hangzhou, 在职) -> 200
态3b 无grant用户(u-nobody, deny-by-default) -> 302 /auth/entry?reason=unauthorized
```

**偏差登记**：未执行 `docker compose --profile bi-web --profile ops-web up` 的真实容器冒烟——镜像构建 + MySQL 初始化耗时超出本批窗口；三态语义已由 TestClient 链路（与 uvicorn 仅差 HTTP 服务器壳）证明，compose 接线正确性由合约测试（`docker compose config` 全绿）覆盖。**首次部署窗口需补做一次真实 compose 冒烟**（态 3 的「SQL 直插 region 记录」步骤在那时执行）。

## 8. 偏差登记（设计稿/提示词 vs 实现）

1. **HTTP 层用 `_http_json`（urllib）而非提示词字面的 httpx**：与 `OrgReadGateway` 的 `request_json` 注入约定保持同一个可替换点（提示词的实操约束原文即「同 OrgReadGateway 的约定」）；httpx 已在依赖但引入它会造出第二套 HTTP fake 形态。
2. **提示页/ops-web 页面未用 Jinja2**：bi-web 的 Jinja2 渲染路径已退役、依赖已拔除（app.py 模块 docstring），仓库亦无 python-multipart。改为：`web/auth-entry.html` 静态页（reason → 固定文案映射，无反射面）+ ops-web 服务端渲染（全部插值过 `html.escape`）+ vanilla JS fetch(JSON)。零新依赖。
3. **session cookie `Secure` 默认关**：HTTPS 反代是 M4（§C3），当前唯一部署形态是纯 HTTP，`Secure` 会使 cookie 完全不可用。预留 `BI_WEB_SESSION_SECURE` 开关，M4 打开。
4. **纯 Bearer 模式下页面拒绝保持 401 而非 302**：「Bearer 路径零行为变化」（验收 4）的必然语义——302 跳转只在 session 通道启用（`BI_WEB_SESSION_SECRET` 已配置）后生效。
5. **Bearer 机器通道不进准入门**：设计稿 §8 大屏/审计通道定位 + 派发文件 §B「大屏 token 白名单位置本批不动」（开放点 #4）。session 身份才过 deny-by-default 准入门。
6. **T0 预存失败 1 个**：hq 种子连带断言（见 §1），已修并登记。
7. **`tests/common/test_db_ddl_gate.py` 白名单 +1**：`common/public_data/bi_authz.py` 作为 `mart-ops-bi-authz-v1` 的 schema 定义模块入列（先例同 `mart_extract_schema.py`）。

## 9. 零 diff 自证（验收 2）

工作区存在并行 B1 批次的未提交改动，故自证方式为**本批改动文件清单 + 写入时间戳**：

- 本批新建：`common/public_data/bi_authz.py`、`common/bi_web/auth.py`、`common/bi_web/authz.py`、`common/bi_web/web/auth-entry.html`、`common/ops_web/`（2 文件）、`docker/integration/ops.seed.yaml`、`tests/common/test_public_data_bi_authz.py`、`tests/common/test_bi_web_auth.py`、`tests/common/test_ops_web_app.py`、本文件
- 本批修改：`common/bi_web/app.py`（仅依赖装配与守卫接线）、`common/bi_web/config.py`（required_scope 可选字段）、`common/public_data/live_migrations.py`（注册 mart-ops-bi-authz-v1）、`common/public_data/cli.py`（load-ops-seed）、`docker-compose.integration.yml`（bi-web 透传变量 + ops-web 服务）、`tests/test_integration_environment.py`、`tests/common/test_db_ddl_gate.py`、`tests/common/test_public_data_org_read.py`
- **未触碰**：`queries.py` / `cards.py`（最后写入 10:47，B1 批）/ `org.seed.json`（16:20，本批前置）/ `frontend/**` / sync-dingtalk / extract-mart 管线 / Nacos dataId 结构 / `credentials/`——本批会话窗口自 16:35 起，以上文件在该窗口内零写入。

## 10. 铁律落点对照

| 铁律 | 实现 | 独立断言 |
|---|---|---|
| 1 deny-by-default | `authz.build_viewer` 无 grant → `admitted=False`；身份闸映射页面 302 `?reason=unauthorized` / API 403；admin 恒准入；seed 仅 admin 一条 | `test_deny_by_default_*`（authz 纯函数 + 应用层）、`ShippedOpsSeedTests` |
| 2 fail-closed | 表读失败/解析异常 → `ViewerResolver` 回 denied（不缓存）；`required_scope` 声明损坏 → 非 admin 拒绝；ops-web 解析异常 → 403 | `test_storage_failure_is_fail_closed_and_never_cached`、`test_resolver_failure_is_fail_closed`、`test_corrupt_*` |
| 3 bi-web 只读不破 | bi-web 仅 `fetch_grants`/`fetch_member_status`（SELECT）；INSERT/DELETE + audit 只在 `bi_authz.upsert_grant/delete_grant/apply_grant_seed`（ops-web 与自举命令调用） | `test_fetch_grants_returns_type_key_pairs`（无 INSERT/DELETE）、ops-web 写 API 测试、合约测试零挂载/无写闸变量 |
| 4 SQL 在代码 | `allowed_regions` 已算不注入；`queries.py` 零 diff（§9） | 本批文件清单自证 |
| 5 纯签名 session | stdlib hmac/hashlib/secrets；`BI_WEB_SESSION_SECRET` 注入；TTL 缓存复用 `_TTLGate` 同款模式（`ViewerResolver`）；零新依赖 | `SessionPureFunctionTests`（过期/篡改/格式错各自分支） |
| 6 凭据零入仓 | 三变量 `${...:-}` 透传；错误泛化；`mask_userid` 脱敏 | 合约测试「零烘焙」断言、`test_*generalized*` |
| 7 两服务共用认证代码 | `common/bi_web/auth.py` + `authz.py` 唯一实现，ops-web import 复用（含 `AUTH_ENTRY_PAGE` 同一文件） | ops-web 测试全部经共用模块 |
| 8 ops-web 双闸 | compose 仅回环 18100 + `require_admin`（session + is_admin） | 合约测试端口断言 + `AdminGateTests` |

## 11. 留待 M3/M4 的钩子清单

- `Viewer.allowed_regions` 已算未用：`build_viewer`/`ViewerResolver` 每请求产出（TTL 60s），查询层注入 + 逐卡片合约单测是 M3；
- `required_scope` 已消费（页面级）；行级 `row_scope=None` 豁免声明未做（M3）；
- `/auth/jsapi-config`（jsapi_ticket TTL 缓存 + SHA1 签名）未做（M4）；
- `/auth/qrcode/*` PC 扫码未做（M4，开放点 #2 以官方现行推荐为准）；
- `BI_WEB_SESSION_SECURE` 开关已预留（M4 HTTPS 反代落地后打开）；
- `auth-entry.html` 的 `?reason=login` 文案位已预留钉钉容器自动取 authCode 的跳转链（M4 前端）；
- `ViewerResolver.failures` 计数已预留，未接诊断端点（可挂到未来 ops-web sync_runs 审计视图）；
- ops-web 后续功能（seed 发布 UI、阈值编辑、整站开关、sync_runs 只读视图）归开放点 #5，权限管理上线后按运维痛点排；
- 真实 compose 冒烟（§7 偏差）首次部署窗口补做。

## 12. 一句话总结

M0–M2 已按派发提示词落地：grant/audit 两表 + admin（王城）自举通道（`load-ops-seed`）、bi-web 钉钉免登骨架（session 优先 / Bearer 兜底逐字不变 / deny-by-default 准入门 / 页面级 required_scope）、ops-web 权限管理（成员/授权/审计三页 + 事务两写写 API + 按区域批量开通，双闸）——**1294 测试全绿（+94，零回归），卡片 SQL 零 diff，铁律 1/2/3 各有独立断言**；行级（M3）与前端体验（M4）的钩子已全部埋好。
