# digital-ops（数字化运营机器人仓库）

钉钉相关运营机器人的统一纳管仓库。目录结构按「业务域 → 平台 → 功能」三层组织：

```
数字化/
└── 钉钉/
    ├── 杭州日报机器人/    # 杭州销售日报：报数监听/18:30提醒/20:00催办DING/组织同步/完成率榜单
    ├── 渠道日报机器人/    # 渠道日报表群：热卖品/采购/销售达成/风控/库存预警 5 个每日播报
    └── 榜单页面/          # 杭州销售完成率榜单托管页源（每日 8:30 生成后发布）
```

## 运行方式（当前：本机执行）

- 运行环境：Windows + `miniconda3` Python；公共数据模块依赖 `requirements.txt` 中的 Python 包。
- 调度：千问办公桌面端 cron 定时任务（5 个），路径指向本仓库
- 页面发布：千问办公 QW Pages（每日 8:30 生成 HTML → 复制到 榜单页面/ → 发布）
- 钉钉凭据：`config.json`（已 gitignore，模板见 `config.example.json`）

## 公共数据设置

- 安装依赖请使用 `requirements.txt`。
- 必须显式设置 `APP_ENV` 为 `test` 或 `production`；两者使用隔离的数据库组。
- `TEST_MODE` 仅用于测试组路由，不能选择数据库。
- 将 `common/public_data/config.example.json` 复制到 `config-local/public-data/`，并让 `PUBLIC_DATA_CONFIG` 指向这份本地契约文件。
- 本地公共数据契约不得记录真实数据库连接信息、钉钉 Base/Sheet ID 或旺店通凭据。

### 受限读取网关（当前状态）

- `common.public_data.dingtalk_read.DingTalkReadGateway` 仅提供钉钉 AI 表的受限读取：OAuth 取 token、表/字段发现、schema 校验与记录分页；它不提供写表、群消息、DING 或 webhook 能力。
- 单元验证使用注入的假 transport，不会访问真实钉钉或旺店通：`python -m unittest tests.common.test_public_data_dingtalk_read -v`。
- `docker-compose.integration.yml` 只启动本地 Ubuntu 测试容器与本地 MySQL 8.4 测试库；运行 `docker compose -f docker-compose.integration.yml up --build --abort-on-container-exit --exit-code-from test-runner` 不会触发真实源同步或外部写入。
- WDT 受限读取、manifest、raw/mart 迁移、同步编排、真实源验收和业务脚本切换至数据库仍未实施；现有机器人尚未切换运行路径。任何调度切换均须由运维人员另行确认。

## 分支与变更流程

- `main`：随时可运行的稳定版本，本机定时任务只执行 main
- 改动流程：改代码 → 本地验证（见各模块 README）→ `git commit` 到 main
- 回滚：`git revert <commit>`，次日定时任务自动生效；紧急时手动立即执行

## 各模块说明

详见各目录内 README：
- `数字化/钉钉/杭州日报机器人/README.md`
- `数字化/钉钉/渠道日报机器人/README_ECS部署指南.md`

## 云迁移

迁移到 GitHub + CI/CD 的步骤见 `docs/云迁移指南.md`。CI 配置已预置：`.github/workflows/ci.yml`。
