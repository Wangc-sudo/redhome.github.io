/* ============================================================================
 * ⚠️ 登记的临时债 —— 后端补齐口径后【整文件删除】
 * ----------------------------------------------------------------------------
 * 为什么还存在：后端 `/api/v1/d/{id}/cards/{card}` 对**部分**卡片尚未产出 CubeSchema §2
 * 的派生指标（shortfall / rate / required_daily），本文件为这些卡补齐展示所需的算式。
 * 按 frontend/README.md 铁律 2，这些算式**只允许出现在本文件**，渲染器里一律禁用；
 * 本文件是唯一例外，且集中、可开关、可整体删除。
 *
 * 开关：VITE_DERIVE=0 关闭（后端补齐后置 0，随后删文件）。
 * 口径来源：bi-ui/CubeSchema.md §2（与 Python 主线同一份文档，本文件不改 Python）。
 *
 * 已知限制（后端补齐前 unavoidable）：
 *   - **工作日一律以后端为准，本地零推算**：业务是大小休（周六默认上班，仅「大休周」
 *     的周六休）叠加法定假与调休，前端不可能知道这些规则，任何本地推算都是在伪造口径
 *     ——比不显示更危险，因为它看起来是对的。故缺日历时 required_daily 一律 null，
 *     UI 按 CubeSchema §5 显示「—」。
 *     参照：2026-09 真机 24 个工作日（dim_calendar），按周一至周五推算只有 22，已确认 22 是错的。
 *   - target 取后端原始字段 `target`，done 取 `done`（部分卡是 `sales`；后端人员榜用
 *     `completed`，本文件不认——没有「已完成」列就不派生）。
 *
 * ⚠️ 已删除：四级告警（p0/p1/p2/ok）分支，2026-09-15 经 team-lead 裁决删除。
 *   理由：告警需求只落在 `kpi_shortfall` / `anomaly_top` 两张卡，这两张卡的 severity
 *   **后端 `common/bi_web/derived.py` 已产出并原样透传**（见 adapter/cardToCube.ts 的
 *   backendDerivedOf）。前端再留一份判定逻辑就是第二处真相，删掉比抹平更干净。
 *   删除前已查证：后端注册表里没有任何一张卡依赖前端算 severity —— 唯一带 target 的
 *   表卡 `table_people_leaderboard` 后端不下发 severity 列，且其「已完成」列名为
 *   completed（本文件不认），因此删除后无卡受影响。
 *   ⇒ 前端**不再产出任何 severity**；需要告警时由后端下发，前端只查表出 chip
 *     （data/severity.ts，该文件在 derive.ts 删除后仍需保留）。
 * ========================================================================== */

import type { CubeRow, CubeSchema, DerivedMetric } from '../types/cube';

/** feature flag：VITE_DERIVE=0 → 整层关闭，渲染器拿到空 derived，退化为「无派生」展示。 */
export const DERIVE_ENABLED: boolean = import.meta.env.VITE_DERIVE !== '0';

/* -------------------------------------------------------------- 工作日口径 */

/**
 * 工作日**只能来自后端**（`dim_calendar`，后端随卡片下发 `total_workdays` /
 * `elapsed_workdays` / `remaining_workdays`）。业务为大小休 + 法定假/调休，前端无从推算，
 * 因此本文件**不提供任何本地日历推算**；取不到即为「不可算」，UI 显示「—」（§5 数据缺陷显式化）。
 */
export const WORKDAY_KEYS = {
  total: 'total_workdays',
  elapsed: 'elapsed_workdays',
  remaining: 'remaining_workdays',
} as const;

/* ------------------------------------------------------------------ 派生算式 */

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/**
 * 所需日销 = target / 当月总工作日（CubeSchema.md §2.3）。
 * 总工作日未知（null / ≤ 0）→ **返回 null**，绝不回落到任何本地推算值。
 */
export function requiredDaily(target: number | null, totalWorkdays: number | null): number | null {
  if (target == null || totalWorkdays == null) return null;
  if (!Number.isFinite(totalWorkdays) || totalWorkdays <= 0) return null;
  return target / totalWorkdays;
}

/** 「已完成」列：后端不同卡叫 done / sales；人员榜的 completed 不在此列（见文件头）。 */
function doneKeyOf(row: CubeRow): 'done' | 'sales' | null {
  if ('done' in row) return 'done';
  if ('sales' in row) return 'sales';
  return null;
}

/**
 * 缺口 = 当期目标总额 − 已完成（**不**乘时间进度，见 CubeSchema.md §2.1）。
 *
 * done 缺失按 0 计：后端 `run_kpi_shortfall` 的 done 是 `COALESCE(SUM(d.done), 0)`，
 * 且左表由 target 驱动 —— 能出现在结果集本身就说明有目标，窗口内无销单 ⇒ 0。
 * （golden `missing_done_counts_as_no_sales`，team-lead 2026-09-15 改判：
 *   告警宁可偏严，也别把「挂零」粉饰成「无数据」。）
 */
export function shortfallOf(target: number | null, done: number | null): number | null {
  if (target == null) return null;
  return target - (done ?? 0);
}

/** 完成率 = done / target（原值保留，封顶只在展示层；target ≤ 0 不可除 → null） */
export function rateOf(target: number | null, done: number | null): number | null {
  if (target == null || target === 0) return null;
  return (done ?? 0) / target;
}

/** 行级派生：算不出就返回 null（渲染器按「无派生」处理，不臆造数字）。 */
export function deriveRow(row: CubeRow): DerivedMetric | null {
  if (!DERIVE_ENABLED) return null;

  const target = num(row['target']);
  if (target == null) return null; // 无目标 → 缺口/完成率/日均全部不可算（§5 显示「—」）

  const doneKey = doneKeyOf(row);
  if (doneKey === null) return null; // 这张卡没有「已完成」列：绝不把「没有该字段」当 0
  const done = num(row[doneKey]) ?? 0; // 列存在但值为 null ⇒ SQL COALESCE 语义 = 0

  // 工作日只认后端字段（dim_calendar）；取不到就是不可算，不做任何本地推算
  const totalWd = num(row[WORKDAY_KEYS.total]);
  const rd = requiredDaily(target, totalWd);

  return {
    target,
    done,
    shortfall: shortfallOf(target, done) ?? undefined,
    requiredDaily: rd ?? undefined,
    progressRate: rateOf(target, done) ?? undefined,
  };
}

/** 整表派生：按 rowKeys（缺失则退回首列取值）逐行写入 derived。 */
export function deriveCube(cube: CubeSchema): CubeSchema {
  if (!DERIVE_ENABLED) return cube;

  const derived: Record<string, DerivedMetric> = { ...(cube.derived ?? {}) };
  let touched = false;

  cube.rows.forEach((row, i) => {
    const key = cube.rowKeys?.[i] ?? String(row[cube.dimensions[0]?.key ?? ''] ?? '');
    if (!key || derived[key]) return;
    const d = deriveRow(row);
    if (d) {
      derived[key] = d;
      touched = true;
    }
  });

  return touched ? { ...cube, derived } : cube;
}
