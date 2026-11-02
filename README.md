# digital-ops（数字化运营平台）

统一的数字化运营仓库，承载钉钉机器人、数据采集与加工、经营看板、榜单页、知识库问答和运维管理面。

## 当前运行方式

生产环境已迁移到云主机，以 Docker 容器组运行；不再依赖办公电脑、本机 cron 或 QW Pages。

| 服务 | 职责 |
|---|---|
| `dingtalk-gateway` | 日报机器人的钉钉 Stream 接入、报数与群消息投递 |
| `dops-scheduler` | 定时任务调度、数据同步、提取加工、播报与知识库索引 |
| `kb-gateway` | 小易知识库问答机器人，独立钉钉 Stream 连接 |
| `dops-bi-web` | BI 看板与小易 Web 会话/API |
| `dops-ops-web` | 运维管理面：管道、名册、组织编排、日历、授权与审计 |
| `dops-pages-web` | 榜单页请求时渲染，带缓存与静态兜底 |
| `dops-nginx` | 域名入口、反向代理、安全头与限流 |
| `dops-ntp` | 云主机时间校准 |

生产编排的版本受控真源是 `docker/cloud/docker-compose.dops.yml`，nginx 配置真源是 `docker/cloud/nginx-pages.conf`。服务器上的 `/opt/dops/` 文件是部署副本，不应绕过仓库单独维护。

## 部署与变更流程

1. `main` 是稳定部署分支；改动通过分支和 Pull Request 合入，不直接提交到 `main`。
2. GitHub Actions 运行 Python 编译、集成测试和前端构建检查，质量门通过后才能合并。
3. 云主机由 systemd timer 每两分钟触发 `ops/deploy.sh`，从配置的 Git 远端拉取 `main`。
4. 部署脚本按需重建镜像、应用数据库迁移、重建长驻服务并执行健康检查；失败时回滚到部署前版本。
5. 回滚代码变更使用 `git revert <commit>`，再按正常 PR 与部署链路发布。

部署远端和分支可通过 `DOPS_REMOTE`、`DOPS_DEPLOY_BRANCH` 配置。灾备机使用 `DOPS_SYNC_ONLY=1`，只同步代码，不启动生产调度或 Stream 连接。

## 配置与机密

- Python 依赖见 `requirements.txt`；生产镜像定义见 `docker/cloud/Dockerfile`。
- 必须显式设置 `APP_ENV=test` 或 `APP_ENV=production`，两套环境使用隔离的数据库配置。
- 本地公共数据契约从 `common/public_data/config.example.json` 复制，并通过 `PUBLIC_DATA_CONFIG` 指向本地文件。
- 密码、令牌、钉钉应用凭据和真实数据源配置不得提交到仓库。
- 云上机密保存在仓库外的 `/opt/dops/app.env`、`/opt/dops/ops-web.env` 和 `/opt/dops/live/`。
- 修改 `env_file` 后必须用 `docker compose up -d --force-recreate` 重建对应容器；单纯 `restart` 不会应用环境变量变化。

## 本地开发与测试

本地开发和集成测试不连接真实生产数据源，也不应向钉钉或生产库写入。

```bash
python -m pip install -r requirements.txt
docker compose -f docker-compose.integration.yml run --rm --build test-runner
```

前端检查：

```bash
cd frontend/bi-react
npm ci
npm run typecheck
npm run test
npm run build
```

`common/ops_web/web` 的 Vue 前端修改后还需执行 `npm run build`，并将更新后的 `common/ops_web/static/dist` 一并提交。

## 目录说明

```text
common/                 机器人、数据管线、BI、运维面和知识库问答后端
frontend/bi-react/      BI 看板前端
docker/cloud/           生产容器编排、镜像与 nginx 配置真源
docker/integration/     集成测试及本地配置种子
ops/                    拉模式部署脚本与 systemd 单元
tests/                  Python 单元与集成测试
docs/                   方案、运行记录与专项说明
数字化/                 早期业务脚本与历史页面（非当前生产调度入口）
```

## 运维入口

- 定时任务、名册、组织、日历和授权等日常管理优先使用 `dops-ops-web`。
- 管道注册与运行状态由调度系统读取；手动触发、停用和审计均应在管理面留痕。
- 真实源同步和提取命令具有显式写入确认参数，不能在未核对环境和目标库时直接执行。
