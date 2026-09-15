/* ============================================================================
 * 跨线口径对拍：前端 derive.ts  ←→  tests/fixtures/derived_golden.json
 * ----------------------------------------------------------------------------
 * 夹具来源：metrics-semantics 产出的用例（24 派生 + 3+ 日历），期望值全部手写
 * 推算（非实现回填），每条带 CubeSchema.md §2.x 出处。
 *
 * 本文件的定位：**度量器**，不是修理工。
 *   - 已对齐的用例 → 真断言，逐位比对（浮点 1e-9）。
 *   - 未对齐的用例 → 走「冲突台账」（characterization），把 TS 当前值与 golden
 *     期望值同时钉住。**任何一侧变动都会让这里变红**，强制重新走裁决流程，
 *     而不是悄悄漂移。
 *
 * ⚠️ 冲突裁决权在 team-lead，裁判是 frontend/bi-ui/CubeSchema.md。
 *    本文件不得为了「跑绿」而改 derive.ts 的口径。
 *
 * 对拍字段范围（2026-09-15 起）：shortfall / rate / required_daily。
 * severity 与日历推导**已不在比对范围内**——不是 skip，是**不再适用**：
 *   - severity：前端分支按 team-lead 裁决删除，真相源只有后端
 *     `common/bi_web/derived.py`（`kpi_shortfall` / `anomaly_top` 后端已下发）。
 *   - 日历：工作日只能来自后端 `dim_calendar`（大小休 + 法定假/调休），前端零推算。
 * ========================================================================== */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import * as deriveNS from './derive';
import { rateOf, requiredDaily, shortfallOf } from './derive';

/* ------------------------------------------------------------------ 夹具加载 */

const GOLDEN_PATH = fileURLToPath(
  new URL('../../../../tests/fixtures/derived_golden.json', import.meta.url),
);

interface MomExpect {
  value: number;
  direction: 'up' | 'down' | 'flat';
}

interface CaseExpect {
  shortfall: number | null;
  rate: number | null;
  required_daily: number | null;
  severity: string | null;
  p1_threshold?: number | null;
  mom?: MomExpect | null;
}

interface GoldenCase {
  name: string;
  source: string;
  note: string;
  inputs: {
    target: number | null;
    done: number | null;
    total_workdays: number | null;
    elapsed_workdays: number | null;
    remaining_workdays: number | null;
    current?: number | null;
    prev?: number | null;
  };
  expect: CaseExpect;
}

interface CalendarCase {
  name: string;
  source: string;
  note: string;
  inputs: {
    calendar: (string | { date: string | null; is_workday: number | null })[];
    as_of: string;
  };
  expect: { total_workdays: number; elapsed_workdays: number; remaining_workdays: number };
}

const golden = JSON.parse(readFileSync(GOLDEN_PATH, 'utf8')) as {
  cases: GoldenCase[];
  calendar_cases: CalendarCase[];
};

/* 注：本文件的用例一律用 fixture 里的 total_workdays 整数喂入，**不喂日期**。 */

/* ------------------------------------------------------------------ TS 侧求值 */

interface TsResult {
  shortfall: number | null;
  rate: number | null;
  required_daily: number | null;
}

/**
 * 用 derive.ts 的**纯函数**跑一条用例。
 *
 * 入参映射刻意保持「喂什么就是什么」，不做任何美化：total_workdays 为 null 就原样传
 * null（requiredDaily 已支持可空 → 日历未知即不可算）。
 */
function tsEval(c: GoldenCase): TsResult {
  const i = c.inputs;
  return {
    shortfall: shortfallOf(i.target, i.done),
    rate: rateOf(i.target, i.done),
    required_daily: requiredDaily(i.target, i.total_workdays),
  };
}

const FIELDS = ['shortfall', 'rate', 'required_daily'] as const;
type Field = (typeof FIELDS)[number];

function close(a: number, b: number): boolean {
  return Math.abs(a - b) < 1e-9;
}

/** 逐字段比对（含 null 语义：null 只能等于 null，绝不等于 0 / false）。 */
function diffOf(c: GoldenCase): Field[] {
  const ts = tsEval(c);
  const exp = c.expect;
  const bad: Field[] = [];

  for (const f of FIELDS) {
    const got = ts[f];
    const want = (exp as unknown as Record<string, unknown>)[f];

    if (want === null || want === undefined) {
      if (got !== null) bad.push(f);
      continue;
    }
    if (got === null) {
      bad.push(f);
      continue;
    }
    if (typeof want === 'number' && typeof got === 'number') {
      if (!close(got, want)) bad.push(f);
      continue;
    }
    if (String(got) !== String(want)) bad.push(f);
  }
  return bad;
}

const cases = golden.cases;
const diverged = cases.map((c) => ({ c, fields: diffOf(c) })).filter((d) => d.fields.length > 0);
const aligned = cases.filter((c) => diffOf(c).length === 0);

const MOM_CASES = cases.filter((c) => 'mom' in c.expect);

/* ------------------------------------------------------------------ 台账常量 */

/**
 * 已登记冲突。**裁决后本清单应逐步清空**；任何新增冲突都会让下面的「冲突集合」测试变红。
 *
 * 当前：**空**。四条历史冲突的归宿 ——
 *   ① missing_target_everything_none：severity 字段已移出比对范围（前端不再产 severity）。
 *   ② missing_done_counts_as_no_sales：team-lead 2026-09-15 改判「后端 SQL 是
 *      COALESCE(SUM(done),0) 且左表由 target 驱动 ⇒ done 缺失 = 真的没开单」→ 改 TS 认
 *      golden（shortfall/rate 的 done 缺失按 0 计），已一致。
 *   ③④ 工作日可空：随「以 dim_calendar 为准、本地零推算」改造消解，已一致。
 */
const REGISTERED_CONFLICTS: Record<string, string> = {};

/* ---------------------------------------------------------------------- 测试 */

describe('golden 夹具自检', () => {
  it('24 条派生用例 + ≥3 条日历用例（B 可能补真实 2026-09 日历用例）', () => {
    expect(cases).toHaveLength(24);
    expect(golden.calendar_cases.length).toBeGreaterThanOrEqual(3);
  });

  it('每条都带 source（可回溯到 CubeSchema.md 条款）', () => {
    for (const c of [...cases, ...golden.calendar_cases]) {
      expect(c.source, c.name).toMatch(/§/);
    }
  });
});

describe('派生用例 · 与 golden 逐位一致（shortfall / rate / required_daily）', () => {
  it.each(aligned)('$name（$source）', (c) => {
    expect(diffOf(c), `${c.name} 分歧字段：${JSON.stringify(tsEval(c))}`).toEqual([]);
  });
});

describe('派生用例 · 冲突台账（不得为跑绿改口径）', () => {
  it('冲突集合必须恰好等于已登记清单（新增或消解都会变红）', () => {
    expect(diverged.map((d) => d.c.name).sort()).toEqual(Object.keys(REGISTERED_CONFLICTS).sort());
  });

  it('每条登记冲突仍真实存在（把 TS 值与 golden 期望同时写进消息，裁决时一眼可读）', () => {
    for (const [name] of Object.entries(REGISTERED_CONFLICTS)) {
      const entry = diverged.find((d) => d.c.name === name);
      expect(entry, `${name} 已不再分歧，请把该用例翻回「逐位一致」组并清空台账`).toBeTruthy();
    }
  });
});

describe('severity：不在比对范围（前端已删分支，真相源只有后端 derived.py）', () => {
  it('derive.ts 不再导出 severityOf —— 若有人把它加回来，这条会变红', () => {
    const ns = deriveNS as unknown as Record<string, unknown>;
    expect(ns['severityOf']).toBeUndefined();
    expect(ns['P1_COEFF']).toBeUndefined();
    expect(ns['P0_MIN_ELAPSED_WORKDAYS']).toBeUndefined();
  });

  it('golden 仍钉着 severity 期望（Python 侧继续对拍，前端只是不再有第二份实现）', () => {
    expect(cases.every((c) => 'severity' in c.expect)).toBe(true);
  });
});

describe('环比 mom（§2.4）', () => {
  it('6 条 mom 用例的派生部分（缺口/完成率/日均）全部一致', () => {
    expect(MOM_CASES).toHaveLength(6);
    for (const c of MOM_CASES) {
      expect(diffOf(c), c.name).toEqual([]);
    }
  });

  it('mom 字段本身 out of scope：环比由后端 ScalarCard 的 delta_pct 产出，TS 不实现', () => {
    // 若后续为对拍补了 momOf，这条会变红 → 提醒把 6 条 mom 期望也纳入比对
    expect((deriveNS as Record<string, unknown>)['momOf']).toBeUndefined();
    expect(MOM_CASES.every((c) => c.expect.mom !== undefined)).toBe(true);
  });
});

/**
 * 日历用例：TS 侧**显式跳过**。
 * 理由（team-lead 2026-09-15 裁决）：工作日真相源是后端 `dim_calendar`（大小休 + 法定假/
 * 调休，2026-09 = 24 天），前端零推算；`calendar_cases` 测的是日历推导，由 Python 侧覆盖。
 * B 若补真实 2026-09 日历用例（24/13/11），会自动落进这个 skip 组，无需改本文件。
 */
describe.skip('日历用例（§2.3 分母 / 剩余工作日）—— 前端不实现：日历由后端 dim_calendar 提供', () => {
  it.each(golden.calendar_cases)('$name', () => {
    /* 不实现：见上方理由 */
  });
});

describe('工作日口径（team-lead 裁决：本地零推算）', () => {
  it('derive.ts 不导出任何本地日历推算函数', () => {
    const ns = deriveNS as unknown as Record<string, unknown>;
    expect(ns['workdaysInMonth']).toBeUndefined();
    expect(ns['elapsedWorkdays']).toBeUndefined();
    expect(ns['remainingWorkdays']).toBeUndefined();
  });

  it('已知日历用例名仍被登记（不因跳过而丢失可见性；新增用例不在此钉死）', () => {
    const names = golden.calendar_cases.map((c) => c.name);
    expect(names).toEqual(
      expect.arrayContaining([
        'calendar_plain_workday_dates',
        'calendar_rows_with_restday_flags',
        'calendar_unknown_rows_are_skipped',
      ]),
    );
  });
});
