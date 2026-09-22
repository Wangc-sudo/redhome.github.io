import { describe, expect, it } from 'vitest';
import { cardToCube, derivedOf, formatCell } from './cardToCube';
import type { CardDef } from '../types';

const NOW = new Date(2026, 8, 15); // 2026-09-15，固定「今天」，保证断言稳定

function card(chart: CardDef['chart'], id = 'c1'): CardDef {
  return { card: id, title: `卡-${id}`, chart, span: 6, on_click: null, params: [] };
}

describe('cardToCube · 五种卡型归一', () => {
  it('scalar：单行 + derived（target/rate 透传，value 视作 done）', () => {
    const cube = cardToCube(
      card('scalar', 'kpi_annual_progress'),
      { chart: 'scalar', value: 21762753.02, target: 760210000.0, rate: 0.02862729, unit: '元' },
      { dashboardId: 'l1-cockpit', now: NOW },
    );

    expect(cube.chart).toBe('scalar');
    expect(cube.rows).toHaveLength(1);
    expect(cube.rowKeys).toEqual(['kpi_annual_progress']);
    expect(cube.rows[0].value).toBe(21762753.02);
    expect(cube.unit).toBe('元');

    const d = derivedOf(cube, 0);
    expect(d?.target).toBe(760210000.0);
    expect(d?.done).toBe(21762753.02);
    expect(d?.progressRate).toBeCloseTo(0.02862729, 8);
  });

  it('scalar：value 为 null → missing，不臆造数字', () => {
    const cube = cardToCube(card('scalar', 'kpi_x'), { chart: 'scalar', value: null, unit: '元' }, { now: NOW });
    expect(cube.rows[0].value).toBeNull();
    expect(derivedOf(cube, 0)?.missing).toBe(true);
  });

  it('line：每个日期一行，每条 series 一个 measure', () => {
    const cube = cardToCube(
      card('line', 'trend_region_daily'),
      {
        chart: 'line',
        dates: ['09-01', '09-02'],
        series: [
          { name: '杭州', data: [1, 2] },
          { name: '绍兴', data: [3, null] },
        ],
        unit: '元',
      },
      { now: NOW },
    );

    expect(cube.chart).toBe('line');
    expect(cube.rows).toHaveLength(2);
    expect(cube.measures.map((m) => m.key)).toEqual(['杭州', '绍兴']);
    expect(cube.rowKeys).toEqual(['09-01', '09-02']);
    expect(cube.rows[0]['杭州']).toBe(1);
    expect(cube.rows[1]['绍兴']).toBeNull(); // 空值原样保留，不补 0
  });

  it('bar：每个类目一行 (category, value)', () => {
    const cube = cardToCube(
      card('bar', 'bar_channel_mtd'),
      { chart: 'bar', categories: ['直播', '猫超'], values: [5619927.19, null], unit: '元' },
      { now: NOW },
    );

    expect(cube.chart).toBe('bar');
    expect(cube.rows).toEqual([
      { category: '直播', value: 5619927.19 },
      { category: '猫超', value: null },
    ]);
    expect(cube.rowKeys).toEqual(['直播', '猫超']);
  });

  it('table：列序/格式以后端为准，首列作行主键', () => {
    const cube = cardToCube(
      card('table', 'table_channel_mtd'),
      {
        chart: 'table',
        columns: [
          { key: 'channel', title: '渠道' },
          { key: 'sales', title: '本月销售额', format: 'wan' },
          { key: 'stores', title: '店铺数', format: 'int' },
        ],
        rows: [
          { channel: '直播', sales: 5619927.19, stores: 5 },
          { channel: '即时零售', sales: 951590.0, stores: 4 },
        ],
      },
      { now: NOW },
    );

    expect(cube.chart).toBe('table');
    expect(cube.columns?.map((c) => c.key)).toEqual(['channel', 'sales', 'stores']);
    expect(cube.dimensions.map((d) => d.key)).toEqual(['channel']);
    expect(cube.measures.map((m) => m.key)).toEqual(['sales', 'stores']);
    expect(cube.rowKeys).toEqual(['直播', '即时零售']);
    expect(cube.rows).toHaveLength(2);
  });

  it('pie：每个扇区一行 (name, value)', () => {
    const cube = cardToCube(
      card('pie', 'pie_sku_mtd'),
      { chart: 'pie', items: [{ name: 'A', value: 1 }, { name: 'B', value: null }], unit: '元' },
      { now: NOW },
    );

    expect(cube.chart).toBe('pie');
    expect(cube.rows).toEqual([
      { name: 'A', value: 1 },
      { name: 'B', value: null },
    ]);
    expect(cube.rowKeys).toEqual(['A', 'B']);
  });
});

describe('cardToCube · 边界', () => {
  it('payload 为 null → 空 CubeSchema（渲染器走 EmptyState，不崩）', () => {
    const cube = cardToCube(card('table', 'x'), null, { now: NOW });
    expect(cube.rows).toEqual([]);
    expect(cube.rowKeys).toEqual([]);
  });

  it('未知 chart → 兜底空表', () => {
    const cube = cardToCube(card('scalar', 'x'), { chart: 'unknown' } as never, { now: NOW });
    expect(cube.rows).toEqual([]);
  });

  it('table 带 target/done 但后端未给 severity → 前端零补算（derive.ts 已删），行字段原样透传', () => {
    const cube = cardToCube(
      card('table', 'table_people_mtd'),
      {
        chart: 'table',
        columns: [
          { key: 'name', title: '姓名' },
          { key: 'target', title: '目标', format: 'wan' },
          { key: 'done', title: '已完成', format: 'wan' },
        ],
        rows: [
          { name: '张伟', target: 800000, done: 0, total_workdays: 24 },
          { name: '陈杰', target: 400000, done: 620000, total_workdays: 24 },
        ],
      },
      { now: NOW },
    );

    // 派生一律后端算：前端不再注入 shortfall/progressRate/requiredDaily，
    // 也不再注入告警；行字段（含后端工作日）原样保留，缺派生渲染「—」
    expect(cube.derived?.['张伟']).toBeUndefined();
    expect(cube.derived?.['陈杰']).toBeUndefined();
    expect(cube.rows[0]['target']).toBe(800000);
    expect(cube.rows[0]['done']).toBe(0);
    expect(cube.rows[0]['total_workdays']).toBe(24);
  });
});

describe('formatCell · 只格式化不换算', () => {
  it('null/空 → —', () => {
    expect(formatCell(null, 'wan')).toBe('—');
    expect(formatCell(undefined, 'int')).toBe('—');
  });
  it('同比率 null（除零/无基数）→ 「—」，绝不显示 0% / -100%（指南 §2 铁律钉死）', () => {
    expect(formatCell(null, 'pct')).toBe('—');
    expect(formatCell(null, 'percent')).toBe('—');
    expect(formatCell(null, 'ratio')).toBe('—');
    // 真实的 0 仍是 0.0%——只有 null 才是「—」
    expect(formatCell(0, 'pct')).toBe('0.0%');
  });
  it('wan → 万；int → 千分位', () => {
    expect(Number(formatCell(5619927.19, 'wan'))).toBeCloseTo(562.0, 1);
    expect(Number(formatCell(12345, 'int').replace(/,/g, ''))).toBe(12345);
  });
  it('无 format → 原样字符串', () => {
    expect(formatCell('直播')).toBe('直播');
  });
});
