# frontend/ · BI 展示层

归属：`digital-ops` 仓库。这里放 **bi-ui（设计系统 + 取数契约）** 与 **bi-react（Vite+React 看板应用）**。

```
frontend/
├─ bi-ui/        # 单一真相源：tokens.css / base.css / components.css + CubeSchema.md + VISUALIZATION.md + MIGRATION.md
└─ bi-react/     # React 应用（端口 18090，dev 代理 /api → 127.0.0.1:18080）
```

## 铁律（评审 checklist，违反即回归）

1. **设计系统单一真相源**：`bi-ui/` 之外不得出现第二套 token / 第三套卡片样式。
2. **派生指标后端化**：渲染器内禁止出现 `shortfall / severity / required_daily / 环比` 任何算式。
   `src/data/derive.ts` 是**登记的临时债**，过渡期集中在此，后端补齐口径后整文件删除。
3. **契约优先**：新卡片先定义 CubeSchema（字段/类型/单位/缺失语义），再写渲染器。
4. **粒度克制**：需求表原文"各页面按固定粒度，不随意加细"，加细粒度要过评审。
5. **取数不依赖组件**：组件只 `useCube`，不直接 `fetch`。

## 三条主线所有权（避免并行开发互相踩文件）

| 主线 | 负责范围 | 不碰 |
|---|---|---|
| A 数据供应链 | `common/public_data/manual_import/**`、`docker/integration/manual-import-templates/**`、`cli.py` 追加子命令、`tests/common/test_manual_import.py` | `common/bi_web/**`、`frontend/**`、`docker/integration/bi.seed.yaml` |
| B 口径与契约 | `common/bi_web/**`（queries/cards/app）、`docker/integration/bi.seed.yaml`、`tests/common/test_bi_web_*` | `common/public_data/**`、`frontend/**` |
| C 产品与前端 | `frontend/bi-react/**`、`frontend/bi-ui/**` | `common/**`、`docker/**`、`tests/**` |

> 跨线改动（如 A 产出的表要给 B 写卡）一律走**契约文件**：A 只负责把数据落进 `mart_ops` 并输出表/字段说明，由 B 写口径；C 只消费 `/api/v1/**` 契约。

## 本地运行

```bash
cd frontend/bi-react
npm install
npm run dev          # http://localhost:18090 ，/api 代理到 18080
npm run typecheck    # 必须 0 error
```

后端契约详见 `bi-ui/CubeSchema.md`；组件化方案详见 `bi-react/ARCHITECTURE.md`；
长期路线图见 `docs/`（roadmap.md / plan.md）。
