import { describe, expect, it } from 'vitest';
import * as deriveNS from './derive';
import {
  DERIVE_ENABLED,
  WORKDAY_KEYS,
  deriveRow,
  rateOf,
  requiredDaily,
  shortfallOf,
} from './derive';

/** 后端 dim_calendar 给的 2026-09 口径（大小休 + 中秋 − 调休 = 24 天），不是本地推算值。 */
const TOTAL_WD = 24;

describe('工作日口径：只认后端，本地零推算', () => {
  it('不再导出任何本地日历推算函数（周一至周五那套已删除）', () => {
    const ns = deriveNS as unknown as Record<string, unknown>;
    expect(ns['workdaysInMonth']).toBeUndefined();
    expect(ns['elapsedWorkdays']).toBeUndefined();
    expect(ns['remainingWorkdays']).toBeUndefined();
  });

  it('所需日销 = 目标 / 后端总工作日；总工作日未知 → null（不回落兜底值）', () => {
    expect(requiredDaily(1_030_000, TOTAL_WD)).toBeCloseTo(42916.6667, 3); // 与真机 kpi_shortfall 一致
    expect(requiredDaily(1_030_000, null)).toBeNull();
    expect(requiredDaily(1_030_000, 0)).toBeNull();
    expect(requiredDaily(null, TOTAL_WD)).toBeNull();
  });
});

describe('severity 分支已删除（team-lead 2026-09-15 裁决）', () => {
  it('derive.ts 不再导出 severityOf —— 告警真相只有后端 derived.py 一处', () => {
    const ns = deriveNS as unknown as Record<string, unknown>;
    expect(ns['severityOf']).toBeUndefined();
    expect(ns['P1_COEFF']).toBeUndefined();
    expect(ns['P0_MIN_ELAPSED_WORKDAYS']).toBeUndefined();
  });

  it('派生结果里不再有 alert 字段（chip 只由后端 severity 驱动）', () => {
    const d = deriveRow({
      name: '张伟',
      target: 800_000,
      done: 0,
      [WORKDAY_KEYS.total]: TOTAL_WD,
    });
    expect(d).not.toBeNull();
    expect(d?.alert).toBeUndefined();
    expect(Object.keys(d ?? {})).not.toContain('alert');
  });
});

describe('缺口 shortfall（CubeSchema §2.1：不锚时间进度）', () => {
  it('= 目标 − 已完成，与日历无关', () => {
    expect(shortfallOf(1000, 250)).toBe(750);
    expect(shortfallOf(1000, 1000)).toBe(0);
    expect(shortfallOf(1000, 1200)).toBe(-200); // 超额为负缺口
  });

  it('done 缺失按 0 计（后端 COALESCE 语义，golden missing_done_counts_as_no_sales）', () => {
    expect(shortfallOf(240, null)).toBe(240);
    expect(rateOf(240, null)).toBe(0);
  });

  it('缺 target → null（不臆造缺口）', () => {
    expect(shortfallOf(null, 100)).toBeNull();
    expect(rateOf(null, 100)).toBeNull();
  });

  it('完成率 = done / target；target 为 0 不可除 → null', () => {
    expect(rateOf(1000, 250)).toBe(0.25);
    expect(rateOf(0, 250)).toBeNull();
  });
});

describe('deriveRow', () => {
  it('有 target/done + 后端工作日 → 产出 shortfall + 完成率 + 所需日均', () => {
    const d = deriveRow({
      name: '张伟',
      target: 800_000,
      done: 0,
      [WORKDAY_KEYS.total]: TOTAL_WD,
    });
    expect(d).not.toBeNull();
    expect(d?.shortfall).toBe(800_000);
    expect(d?.requiredDaily).toBeCloseTo(800_000 / TOTAL_WD, 6);
    expect(d?.progressRate).toBe(0);
  });

  it('done 为 null 但列存在 → 按 0 计（不静默留空、不当不可算）', () => {
    const d = deriveRow({ name: '张伟', target: 240, done: null });
    expect(d?.shortfall).toBe(240);
    expect(d?.progressRate).toBe(0);
  });

  it('没有「已完成」列（后端人员榜用 completed）→ 不派生，绝不把「没字段」当 0', () => {
    expect(deriveRow({ name: '甲', target: 1000, completed: 600 })).toBeNull();
  });

  it('没有日历字段 → 日均不可算（undefined），缺口照常可算', () => {
    const d = deriveRow({ name: '张伟', target: 800_000, done: 0 });
    expect(d?.requiredDaily).toBeUndefined();
    expect(d?.shortfall).toBe(800_000); // 缺口与日历无关
  });

  it('没有 target → 返回 null（渲染器按「无派生」处理）', () => {
    expect(deriveRow({ name: '渠道', sales: 123 })).toBeNull();
  });

  it('has_fact 不是前端的语义开关：无事实行也按 done=0 派生（与 golden row_cases 数值一致）', () => {
    // 后端 2026-09-15 新增 has_fact，但它**由后端决定语义**：后端若判 p0 会直接下发
    // severity；前端不另设分支，避免与后端出现第二处口径。
    const d = deriveRow({ name: '无销单人', target: 240, done: null, has_fact: false });
    expect(d?.shortfall).toBe(240);
    expect(d?.progressRate).toBe(0);
  });

  it('开关关闭时整层不产出（VITE_DERIVE=0）', () => {
    // DERIVE_ENABLED 由 import.meta.env 决定；测试环境默认开启，这里只断言开关可达
    expect(typeof DERIVE_ENABLED).toBe('boolean');
  });
});
