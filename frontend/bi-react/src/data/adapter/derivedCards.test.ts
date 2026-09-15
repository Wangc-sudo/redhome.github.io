import { describe, expect, it } from 'vitest';
import { cardToCube } from './cardToCube';
import * as deriveNS from '../derive';
import type { CardDef, TablePayload } from '../types';

/**
 * 后端已产出派生口径的两张卡（CubeSchema §2）：
 *   kpi_shortfall  params_schema {region: regions, month: months}
 *   anomaly_top    params_schema {month: months}
 * payload 形状取自 common/bi_web/queries.py::run_kpi_shortfall / run_anomaly_top
 * （columns 由 shortfall_columns() 给出，行字段由 shortfall_rows() 给出）。
 *
 * 核心断言：**后端算好的 severity/required_daily 原样透传**，不经过 derive.ts
 * 二次加工 —— 否则会出现「后端说 p2、前端显示 p0」的口径漂移。
 */

const card = (id: string): CardDef => ({
  card: id,
  title: id === 'kpi_shortfall' ? '目标缺口与告警' : '今日跟进（缺口 TOP10）',
  chart: 'table',
  span: 6,
  on_click: null,
  params: id === 'kpi_shortfall' ? ['region', 'month'] : ['month'],
});

const COLUMNS = [
  { key: 'name', title: '区域' },
  { key: 'target', title: '月目标', format: 'wan' as const },
  { key: 'done', title: '已完成', format: 'wan' as const },
  { key: 'shortfall', title: '缺口', format: 'wan' as const },
  { key: 'rate', title: '完成率', format: 'percent' as const },
  { key: 'required_daily', title: '所需日均', format: 'wan' as const },
  { key: 'severity', title: '告警', format: 'severity' as const },
];

const shortfallPayload: TablePayload = {
  chart: 'table',
  unit: '元',
  month: '2026-09',
  grain: '区域',
  as_of: '2026-09-15',
  severity_domain: ['p0', 'p1', 'p2', 'ok'],
  columns: COLUMNS,
  rows: [
    {
      name: '杭州', target: 32000000.0, done: 26298400.0, shortfall: 5701600.0, rate: 0.821825,
      required_daily: 1454545.45, severity: 'p2', remaining_workdays: 11, elapsed_workdays: 11,
    },
    {
      // 故意与前端口径冲突的一行：done=0 → derive.ts 会判 p0，后端给的是 p2
      name: '绍兴', target: 12000000.0, done: 0.0, shortfall: 12000000.0, rate: 0.0,
      required_daily: 545454.55, severity: 'p2', remaining_workdays: 11, elapsed_workdays: 11,
    },
    {
      // 数据缺陷：无目标 → 缺口不可算（§5 显式化，不臆造告警）
      name: '宁波', target: null, done: 3000000.0, shortfall: null, rate: null,
      required_daily: null, severity: 'ok', remaining_workdays: 11, elapsed_workdays: 11,
    },
  ],
};

const anomalyPayload: TablePayload = {
  ...shortfallPayload,
  limit: 10,
  columns: [{ key: 'rank', title: '名次' }, ...COLUMNS],
  rows: [
    {
      rank: 1, name: '绍兴', target: 12000000.0, done: 0.0, shortfall: 12000000.0, rate: 0.0,
      required_daily: 545454.55, severity: 'p2', remaining_workdays: 11, elapsed_workdays: 11,
    },
    {
      rank: 2, name: '杭州', target: 32000000.0, done: 26298400.0, shortfall: 5701600.0, rate: 0.821825,
      required_daily: 1454545.45, severity: 'p2', remaining_workdays: 11, elapsed_workdays: 11,
    },
  ],
};

describe('kpi_shortfall · 后端派生口径透传', () => {
  const cube = cardToCube(card('kpi_shortfall'), shortfallPayload);

  it('表格骨架：列序/格式/as_of/unit 全部按后端', () => {
    expect(cube.chart).toBe('table');
    expect(cube.unit).toBe('元');
    expect(cube.asOf).toBe('2026-09-15');
    expect(cube.columns?.map((c) => c.key)).toEqual([
      'name', 'target', 'done', 'shortfall', 'rate', 'required_daily', 'severity',
    ]);
    expect(cube.columns?.find((c) => c.key === 'severity')?.format).toBe('severity');
    expect(cube.rowKeys).toEqual(['杭州', '绍兴', '宁波']);
    expect(cube.rows).toHaveLength(3);
  });

  it('severity 原样透传：前端已无判定逻辑，不可能二次加工', () => {
    const sx = cube.derived?.['绍兴'];

    // 后端给 p2：若前端还有一份口径，这行（done=0 且已过 11 个工作日）会被判成 p0
    expect(sx?.alert?.severity).toBe('p2');
    expect(sx?.alert?.label).toBe('P2');
    expect(sx?.alert?.reason).not.toBe('');

    // 判定函数已从 derive.ts 删除（team-lead 2026-09-15 裁决）；加回来这条就变红
    expect((deriveNS as Record<string, unknown>)['severityOf']).toBeUndefined();
  });

  it('required_daily / shortfall / rate 用后端值（不是本地重算值）', () => {
    const sx = cube.derived?.['绍兴'];
    // 本地重算 = 12000000/22 = 545454.5454…，后端给 545454.55
    expect(sx?.requiredDaily).toBe(545454.55);
    expect(sx?.shortfall).toBe(12000000.0);
    expect(sx?.progressRate).toBe(0);
    expect(cube.derived?.['杭州']?.progressRate).toBe(0.821825);
  });

  it('无目标行：不可算显式标注（missing），不臆造告警', () => {
    const nb = cube.derived?.['宁波'];
    expect(nb?.missing).toBe(true);
    expect(nb?.shortfall).toBeUndefined();
    expect(nb?.requiredDaily).toBeUndefined();
    expect(nb?.alert?.severity).toBe('ok');
  });
});

describe('anomaly_top · 名次列 + 行主键', () => {
  const cube = cardToCube(card('anomaly_top'), anomalyPayload);

  it('首列是 rank，但行主键仍取 name（不能拿名次当主键）', () => {
    expect(cube.columns?.[0].key).toBe('rank');
    expect(cube.rowKeys).toEqual(['绍兴', '杭州']);
    expect(cube.rows[0]['rank']).toBe(1);
  });

  it('派生按 name 索引，severity 同样原样透传', () => {
    expect(cube.derived?.['绍兴']?.alert?.severity).toBe('p2');
    expect(cube.derived?.['杭州']?.requiredDaily).toBe(1454545.45);
  });
});

/**
 * 真机样本：2026-09-15 直连测试库跑 queries.run_kpi_shortfall 抓到的真实响应
 * （后端默认 grain=person，所以列里带「部门」；elapsed=13 / remaining=11）。
 * 用它钉死「后端浮点数原样透传」——本地重算会得到不同的 required_daily。
 */
const realShortfall: TablePayload = {
  chart: 'table',
  unit: '元',
  month: '2026-09',
  grain: 'person',
  as_of: '2026-09-15',
  severity_domain: ['p0', 'p1', 'p2', 'ok'],
  columns: [
    { key: 'name', title: '姓名' },
    { key: 'dept', title: '部门' },
    { key: 'target', title: '月目标', format: 'wan' },
    { key: 'done', title: '已完成', format: 'wan' },
    { key: 'shortfall', title: '缺口', format: 'wan' },
    { key: 'rate', title: '完成率', format: 'percent' },
    { key: 'required_daily', title: '所需日均', format: 'wan' },
    { key: 'severity', title: '告警', format: 'severity' },
  ],
  rows: [
    {
      name: '李树军', dept: '线下运营中心', target: 1030000.0, done: 60454.0, shortfall: 969546.0,
      rate: 0.058693203883495144, required_daily: 42916.666666666664, severity: 'p1',
      remaining_workdays: 11, elapsed_workdays: 13,
    },
    {
      name: '卢炳华', dept: '滨萧', target: 1019000.0, done: 225845.0, shortfall: 793155.0,
      rate: 0.22163395485770362, required_daily: 42458.333333333336, severity: 'p1',
      remaining_workdays: 11, elapsed_workdays: 13,
    },
  ],
};

describe('kpi_shortfall · 真机 payload（2026-09-15 直连测试库抓取）', () => {
  const cube = cardToCube(card('kpi_shortfall'), realShortfall);

  it('列定义含「部门」，行主键 = 姓名', () => {
    expect(cube.columns?.map((c) => c.key)).toEqual([
      'name', 'dept', 'target', 'done', 'shortfall', 'rate', 'required_daily', 'severity',
    ]);
    expect(cube.rowKeys).toEqual(['李树军', '卢炳华']);
    expect(cube.asOf).toBe('2026-09-15');
    expect(cube.unit).toBe('元');
  });

  it('后端浮点数原样透传：required_daily = 42916.666…（本地按 22 工作日重算是 46818.18）', () => {
    const d = cube.derived?.['李树军'];
    expect(d?.requiredDaily).toBe(42916.666666666664);
    expect(d?.shortfall).toBe(969546.0);
    expect(d?.progressRate).toBe(0.058693203883495144);
    expect(d?.alert?.severity).toBe('p1');
    expect(d?.alert?.label).toBe('P1');
  });

  it('后端工作日口径（24 工作日）不被前端 22 覆盖', () => {
    // 后端 elapsed 13 + remaining 11 = 24；前端 Mon-Fri 口径算 22。
    // 这里不断言谁对，只断言**行里的原值被保留**，口径对拍见 derived_golden.json。
    expect(cube.rows[0]['elapsed_workdays']).toBe(13);
    expect(cube.rows[0]['remaining_workdays']).toBe(11);
  });
});

/**
 * `has_fact`（后端 2026-09-15 新增，docs/derived-metrics.md 登记为第 5 类缺陷）。
 *
 * ⚠️ **后端消息与 golden `row_cases` 目前互相矛盾，已上报 team-lead 待裁决**：
 *   - metrics-semantics 的消息：has_fact=false ⇒ done/shortfall/rate/required_daily/severity
 *     全 null ⇒ 前端「—」、**不进 p0**；
 *   - 同一份 golden 的 `row_cases.row_no_fact_with_target_is_also_p0`：has_fact=false 仍期望
 *     shortfall=240 / rate=0 / severity=**p0**（"LEFT JOIN 右表为空 = 挂零的另一名字"）。
 *
 * 前端**不替后端选语义**，只守住两条不变量（任一侧改口径都会变红，强制重走裁决）：
 *   ① 后端给了 severity ⇒ 原样透传，含 p0 chip，前端绝不改判；
 *   ② 后端没给 severity ⇒ 交 derive.ts，done 列存在即按 0 ⇒ 缺口 = 目标（偏严，
 *      与 golden row_cases 的数值一致；team-lead 改判精神：宁可偏严也不把挂零粉饰成无数据）。
 */
describe('has_fact：后端怎么判，前端怎么显示（不替后端选语义）', () => {
  const cube = cardToCube(card('kpi_shortfall'), {
    chart: 'table',
    unit: '元',
    month: '2026-09',
    as_of: '2026-09-15',
    columns: [
      { key: 'name', title: '姓名' },
      { key: 'target', title: '月目标', format: 'wan' },
      { key: 'done', title: '已完成', format: 'wan' },
      { key: 'shortfall', title: '缺口', format: 'wan' },
      { key: 'severity', title: '告警', format: 'severity' },
    ],
    rows: [
      // 形态 A：后端判了 p0（无事实行 = 挂零）→ 原样透传
      { name: '无销单人', target: 240, done: 0, shortfall: 240, severity: 'p0', has_fact: false },
      // 形态 B：后端把指标全置 null、severity=null → 前端按 done=0 派生
      { name: '未同步人', target: 240, done: null, shortfall: null, severity: null, has_fact: false },
    ],
  });

  it('① 后端给了 severity ⇒ 原样透传 chip，不重算', () => {
    const d = cube.derived?.['无销单人'];
    expect(d?.alert?.severity).toBe('p0');
    expect(d?.shortfall).toBe(240);
  });

  it('② 后端没给 severity ⇒ derive.ts 按 done=0 补缺口（偏严，与 golden row_cases 数值一致）', () => {
    const d = cube.derived?.['未同步人'];
    expect(d?.shortfall).toBe(240); // 缺口 = 目标 − 0
    expect(d?.progressRate).toBe(0);
    expect(d?.alert).toBeUndefined(); // 前端不产 severity：告警只能来自后端
  });
});

describe('回归：没有 severity 的表仍走 derive.ts（只补缺口/完成率/日均，不补告警）', () => {
  it('table_people_leaderboard 这类后端未派生的表，前端继续补口径', () => {
    const cube = cardToCube(
      { card: 'table_people_mtd', title: '人员本月达成', chart: 'table', span: 12, on_click: null, params: [] },
      {
        chart: 'table',
        columns: [
          { key: 'name', title: '姓名' },
          { key: 'target', title: '目标', format: 'wan' },
          { key: 'done', title: '已完成', format: 'wan' },
        ],
        // 后端会随行下发工作日（dim_calendar）；前端不推算，缺了就不可算
        rows: [
          {
            name: '张伟',
            target: 800000,
            done: 0,
            total_workdays: 24,
            elapsed_workdays: 13,
            remaining_workdays: 11,
          },
        ],
      },
      { now: new Date(2026, 8, 15) },
    );
    const d = cube.derived?.['张伟'];
    expect(d?.shortfall).toBe(800000);
    expect(d?.progressRate).toBe(0);
    expect(d?.requiredDaily).toBeCloseTo(800000 / 24, 6); // 24 = 后端 dim_calendar
    expect(d?.alert).toBeUndefined(); // 告警归后端，前端不自造
  });
});
