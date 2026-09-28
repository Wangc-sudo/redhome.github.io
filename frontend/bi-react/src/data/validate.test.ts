/**
 * 运行时数据校验（validate.ts）+ 适配层降级行为。
 * ---------------------------------------------------------------------------
 * 守三件事：
 *   ① 合法 payload 零 issue（各 chart 形状全覆盖）；
 *   ② 畸形 payload（缺数组/坏行/域外 severity/序列错位）逐条上报；
 *   ③ 校验不打断渲染：cardToCube 对坏 payload 降级（坏行丢弃、console.warn
 *      上报），绝不抛异常。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cardToCube } from './adapter/cardToCube';
import type { CardDef } from './types';
import { sanitizeTableRows, validateCardPayload } from './validate';

afterEach(() => vi.restoreAllMocks());

const CARD: CardDef = {
  card: 'kpi_shortfall',
  title: '目标缺口与告警',
  chart: 'table',
  span: 6,
  on_click: null,
  params: [],
};

describe('validateCardPayload：合法 payload 零 issue', () => {
  it('table / line / bar / pie / scalar 全形状通过', () => {
    expect(
      validateCardPayload({
        chart: 'table',
        columns: [{ key: 'name', title: '姓名' }],
        rows: [{ name: '甲', severity: 'p0' }],
        severity_domain: ['p0', 'p1', 'p2', 'ok'],
      }),
    ).toEqual([]);
    expect(
      validateCardPayload({
        chart: 'line',
        dates: ['09-21', '09-22'],
        series: [{ name: '销量', data: [1, null] }],
      }),
    ).toEqual([]);
    expect(validateCardPayload({ chart: 'bar', categories: ['A'], values: [1] })).toEqual([]);
    expect(validateCardPayload({ chart: 'pie', items: [{ name: 'A', value: 1 }] })).toEqual([]);
    expect(validateCardPayload({ chart: 'scalar', value: 42 })).toEqual([]);
  });
});

describe('validateCardPayload：畸形 payload 逐条上报', () => {
  it('非对象 payload / 未知 chart', () => {
    expect(validateCardPayload(null)).toHaveLength(1);
    expect(validateCardPayload('x')[0]).toContain('expected object');
    expect(validateCardPayload({ chart: 'radar' })[0]).toContain("unknown chart 'radar'");
  });

  it('table：列/行形状错误与单元格非法类型', () => {
    const issues = validateCardPayload({
      chart: 'table',
      columns: 'not-array',
      rows: [null, { name: { nested: true } }],
    });
    expect(issues.some((s) => s.includes('table.columns'))).toBe(true);
    expect(issues.some((s) => s.includes('table.rows[0]: expected object row'))).toBe(true);
    expect(issues.some((s) => s.includes('table.rows[1].name: unsupported cell type'))).toBe(true);
  });

  it('table：severity 域外值按 severity_domain 上报（不硬编码值域）', () => {
    const issues = validateCardPayload({
      chart: 'table',
      columns: [{ key: 'severity', title: '告警' }],
      rows: [{ severity: 'p9' }],
      severity_domain: ['p0', 'p1', 'p2', 'ok'],
    });
    expect(issues).toEqual(["table.rows[0].severity: 'p9' outside severity_domain"]);
  });

  it('line/bar：序列与轴长度错位', () => {
    expect(
      validateCardPayload({
        chart: 'line',
        dates: ['09-21', '09-22'],
        series: [{ name: '销量', data: [1] }],
      })[0],
    ).toContain('length 1 != dates length 2');
    expect(
      validateCardPayload({ chart: 'bar', categories: ['A', 'B'], values: [1] })[0],
    ).toContain('length 1 != categories length 2');
  });

  it('scalar：value 非数值', () => {
    expect(validateCardPayload({ chart: 'scalar', value: '42' })[0]).toContain('scalar.value');
  });
});

describe('降级行为：校验不打断渲染', () => {
  it('sanitizeTableRows 丢弃非对象行、保留合法行', () => {
    expect(sanitizeTableRows([{ name: '甲' }, null, 'x', { name: '乙' }])).toEqual([
      { name: '甲' },
      { name: '乙' },
    ]);
    expect(sanitizeTableRows('not-array')).toEqual([]);
  });

  it('cardToCube 对坏 payload 不抛异常：坏行丢弃 + console.warn 上报', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const cube = cardToCube(CARD, {
      chart: 'table',
      columns: [{ key: 'name', title: '姓名' }],
      rows: [{ name: '甲' }, null, { name: '乙' }] as unknown as Record<string, unknown>[],
    });
    expect(cube.rows).toHaveLength(2); // null 行被丢弃
    expect(warn).toHaveBeenCalledOnce();
    expect(String(warn.mock.calls[0][0])).toContain('kpi_shortfall');
  });

  it('合法 payload 不告警（线上静默 = 无 issue）', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    cardToCube(CARD, {
      chart: 'table',
      columns: [{ key: 'name', title: '姓名' }],
      rows: [{ name: '甲' }],
    });
    expect(warn).not.toHaveBeenCalled();
  });
});
