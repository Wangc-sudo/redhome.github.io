// @vitest-environment jsdom
/**
 * has_fact 诊断角标（roadmap 2026-09-15 P0；裁决 #3：字段保留作诊断，不参与 severity）。
 * ---------------------------------------------------------------------------
 * 守两件事：
 *   ① has_fact=false 的行渲染中性「挂零」角标（bi-ui .badge-defect），
 *      has_fact=true / 无该字段的行不渲染；
 *   ② 角标**只渲染**：后端给的 severity chip（颜色/文案）与数值一律原样，绝不改判。
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { cardToCube } from '../../data/adapter/cardToCube';
import type { CardDef, TablePayload } from '../../data/types';
import { TableCard } from './TableCard';

afterEach(cleanup);

const CARD: CardDef = {
  card: 'kpi_shortfall',
  title: '目标缺口与告警',
  chart: 'table',
  span: 6,
  on_click: null,
  params: ['month'],
};

function payload(rows: TablePayload['rows']): TablePayload {
  return {
    chart: 'table',
    unit: '元',
    month: '2026-09',
    as_of: '2026-09-15',
    columns: [
      { key: 'name', title: '姓名' },
      { key: 'target', title: '月目标', format: 'wan' },
      { key: 'done', title: '已完成', format: 'wan' },
      { key: 'severity', title: '告警', format: 'severity' },
    ],
    rows,
  };
}

describe('has_fact 诊断角标：仅渲染，不参与判定', () => {
  it('has_fact=false → 「挂零」角标挂在该人姓名格；true / 无字段 → 不渲染', () => {
    const cube = cardToCube(
      CARD,
      payload([
        { name: '无销单人', target: 240, done: 0, severity: 'p0', has_fact: false },
        { name: '正常人', target: 240, done: 100, severity: 'ok', has_fact: true },
        { name: '无字段人', target: 240, done: 50, severity: 'ok' },
      ]),
    );
    // 适配层原样透传诊断字段（不重算、不丢）
    expect(cube.rows[0]['has_fact']).toBe(false);
    expect(cube.rows[1]['has_fact']).toBe(true);

    render(<TableCard cube={cube} />);

    const badges = screen.getAllByText('挂零');
    expect(badges).toHaveLength(1); // 只有 has_fact=false 那一行
    expect(badges[0].className).toContain('badge-defect');
    expect(badges[0].closest('tr')?.textContent).toContain('无销单人');
  });

  it('角标不改判：后端给的 p0 chip（色/文案）与数值原样透传', () => {
    const cube = cardToCube(
      CARD,
      payload([{ name: '无销单人', target: 240, done: 0, severity: 'p0', has_fact: false }]),
    );
    render(<TableCard cube={cube} />);

    // 告警 chip 仍是后端判的 p0（severity.ts 查表文案），颜色类名不变
    const chip = screen.getByText('P0');
    expect(chip.closest('.alert-chip')?.className).toContain('p0');
    expect(screen.getByText('零动销，今日跟进')).toBeTruthy();
    // 数值原样：done=0 → 「0」（wan 格式化），缺口/告警不因角标而变
    const row = screen.getByText('挂零').closest('tr');
    expect(row?.textContent).toContain('0');
    // 角标 title 说清「不影响告警判定」
    expect(screen.getByText('挂零').getAttribute('title')).toContain('不影响告警判定');
  });

  it('没有 severity 列的表也能挂角标（角标与告警列解耦）', () => {
    const cube = cardToCube(CARD, {
      chart: 'table',
      columns: [
        { key: 'name', title: '姓名' },
        { key: 'done', title: '已完成', format: 'wan' },
      ],
      rows: [{ name: '无销单人', done: 0, has_fact: false }],
    });
    render(<TableCard cube={cube} />);
    expect(screen.getAllByText('挂零')).toHaveLength(1);
    expect(screen.queryByText('P0')).toBeNull(); // 无 severity ⇒ 无 chip，前端不自造
  });

  it('anomaly_top 形状（首列 rank）：角标仍挂在 name 列而不是名次列', () => {
    const cube = cardToCube(CARD, {
      chart: 'table',
      columns: [
        { key: 'rank', title: '名次' },
        { key: 'name', title: '姓名' },
        { key: 'severity', title: '告警', format: 'severity' },
      ],
      rows: [{ rank: 1, name: '无销单人', severity: 'p0', has_fact: false }],
    });
    render(<TableCard cube={cube} />);
    const badge = screen.getByText('挂零');
    const cell = badge.closest('td');
    expect(cell?.textContent).toContain('无销单人');
    expect(cell?.textContent).not.toBe('1');
  });
});
