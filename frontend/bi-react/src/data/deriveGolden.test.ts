/* ============================================================================
 * 跨线口径守卫：derive.ts 已删除，前端不再有任何派生实现
 * ----------------------------------------------------------------------------
 * 历史：本文件曾是「前端 derive.ts ←→ tests/fixtures/derived_golden.json」的逐位
 * 对拍器（24 条派生用例在 shortfall / rate / required_daily 上全部一致，冲突台账
 * 清空）。2026-09-16 后端 common/bi_web/derived.py 已就绪且对拍一致，derive.ts 按
 * 路线图 P3（docs/bi-roadmap-2026-09-15.md §5）整文件删除 —— **逐位对拍职责完全移交
 * Python 侧**（tests/ 跑同一份 golden 夹具），前端不再有可比对的第二份实现。
 *
 * 本文件现在的定位：**防回归守卫**（度量器，不是修理工）。原守卫钉的是「derive.ts
 * 不得再导出 severityOf / 本地日历函数」；derive.ts 既已整文件删除，守卫升级为
 * 文件级断言，防回归语义不变、覆盖面更大：
 *   ① derive.ts / derive.test.ts 不存在（谁重建谁变红）；
 *   ② src 下没有任何文件 import derive 模块（含动态 import）；
 *   ③ src 下没有任何文件重新导出本地派生 / 日历 / 告警函数 —— 派生一律后端算
 *      （docs/derived-metrics.md 铁律），在**任何**文件里加回来都会变红；
 *   ④ golden 夹具完整：24 派生 + ≥3 日历、每条带 source、severity 期望仍在
 *      （Python 侧继续对拍，前端只是不再有第二份实现）。
 * ========================================================================== */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

/* ------------------------------------------------------------------ 夹具加载 */

const GOLDEN_PATH = fileURLToPath(
  new URL('../../../../tests/fixtures/derived_golden.json', import.meta.url),
);

interface CalendarCase {
  name: string;
  source: string;
}

const golden = JSON.parse(readFileSync(GOLDEN_PATH, 'utf8')) as {
  cases: { name: string; source: string; expect: Record<string, unknown> }[];
  calendar_cases: CalendarCase[];
};

/* ---------------------------------------------------------------- src 扫描 */

const SRC_ROOT = fileURLToPath(new URL('..', import.meta.url)); // src/

/** 递归收集 src 下全部 .ts/.tsx 源文件（含测试文件：守卫对被测代码与测试一视同仁）。 */
function collectSources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...collectSources(full));
    else if (/\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

const SOURCES = collectSources(SRC_ROOT);

/** 静态 / 动态 import 一个「以 /derive 结尾的相对模块」（'../derive'、'./derive'）。 */
const IMPORT_DERIVE = /\b(?:from|import)\s*(?:\(\s*)?['"](?:\.{1,2}\/)+derive['"]/;

/**
 * 裁决钉死的「前端不得再有」符号（roadmap 裁决 #3/#5 + 派生后端化铁律）。
 * 在任何 src 文件里以 export 形式重新出现即变红。
 */
const FORBIDDEN_EXPORTS = [
  // 本地日历推算（裁决 #5：真相源只有 dim_calendar，前端零推算）
  'workdaysInMonth',
  'elapsedWorkdays',
  'remainingWorkdays',
  // 前端 severity 判定（裁决：真相源只有后端 derived.py）
  'severityOf',
  'P1_COEFF',
  'P0_MIN_ELAPSED_WORKDAYS',
  // 过渡派生层算式（已随 derive.ts 整文件删除）
  'shortfallOf',
  'rateOf',
  'requiredDaily',
  'deriveRow',
  'deriveCube',
  'momOf',
] as const;

function exportPattern(name: string): RegExp {
  return new RegExp(`\\bexport\\s+(?:async\\s+)?(?:const|let|var|function|class)\\s+${name}\\b`);
}

/* ---------------------------------------------------------------------- 守卫 */

describe('derive.ts 已整文件删除（路线图 P3；后端 derived.py 为唯一真相源）', () => {
  it('derive.ts / derive.test.ts 不存在', () => {
    expect(existsSync(join(SRC_ROOT, 'data', 'derive.ts'))).toBe(false);
    expect(existsSync(join(SRC_ROOT, 'data', 'derive.test.ts'))).toBe(false);
  });

  it('src 下没有任何文件 import derive 模块（含动态 import）', () => {
    for (const file of SOURCES) {
      const code = readFileSync(file, 'utf8');
      expect(IMPORT_DERIVE.test(code), `${file} 仍在 import derive 模块`).toBe(false);
    }
  });

  it('src 下没有任何文件重新导出本地派生 / 日历 / 告警函数（裁决 #3/#5）', () => {
    for (const file of SOURCES) {
      const code = readFileSync(file, 'utf8');
      for (const name of FORBIDDEN_EXPORTS) {
        expect(exportPattern(name).test(code), `${file} 重新导出了 ${name}`).toBe(false);
      }
    }
  });
});

describe('golden 夹具自检（对拍职责在 Python 侧，前端守夹具完整）', () => {
  it('24 条派生用例 + ≥3 条日历用例', () => {
    expect(golden.cases).toHaveLength(24);
    expect(golden.calendar_cases.length).toBeGreaterThanOrEqual(3);
  });

  it('每条都带 source（可回溯到 CubeSchema.md 条款）', () => {
    for (const c of [...golden.cases, ...golden.calendar_cases]) {
      expect(c.source, c.name).toMatch(/§/);
    }
  });

  it('golden 仍钉着 severity 期望（Python 侧继续对拍，前端只是不再有第二份实现）', () => {
    expect(golden.cases.every((c) => 'severity' in c.expect)).toBe(true);
  });

  it('mom 用例仍在夹具里（环比由后端 ScalarCard delta_pct 产出，前端不实现 momOf）', () => {
    const momCases = golden.cases.filter((c) => 'mom' in c.expect);
    expect(momCases).toHaveLength(6);
  });
});

/**
 * 日历用例：TS 侧**显式跳过**。
 * 理由（team-lead 2026-09-15 裁决 #5）：工作日真相源是后端 `dim_calendar`
 * （大小休 + 法定假/调休，2026-09 = 24 天），前端零推算；`calendar_cases` 测的是
 * 日历推导，由 Python 侧覆盖。新增日历用例会自动落进这个 skip 组，无需改本文件。
 */
describe.skip('日历用例（§2.3 分母 / 剩余工作日）—— 前端不实现：日历由后端 dim_calendar 提供', () => {
  it.each(golden.calendar_cases)('$name', () => {
    /* 不实现：见上方理由 */
  });
});

describe('工作日口径（裁决 #5：本地零推算）', () => {
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
