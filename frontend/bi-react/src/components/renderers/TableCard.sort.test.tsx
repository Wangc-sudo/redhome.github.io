// @vitest-environment jsdom
/**
 * 表格列排序（三态：升序 → 降序 → 恢复后端原序）。
 * ---------------------------------------------------------------------------
 * 守四件事：
 *   ① 数值列点击表头升/降/还原三态循环，aria-sort 同步；
 *   ② 空值（null）无论升序降序都沉底；
 *   ③ severity 列按 p0>p1>p2>ok 级别排（不按字符串字典序）；
 *   ④ derived.missing（不可算）行不参与排序恒沉底；合计行（tfoot）不受排序影响。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { cardToCube, formatCell } from '../../data/adapter/cardToCube';
import type { CardDef, TablePayload } from '../../data/types';
import { TableCard } from './TableCard';

afterEach(cleanup);

const CARD: CardDef = {
  card: 'kpi_shortfall',
  title: '目标缺口与告警',
  chart: 'table',
  span: 6,
  on_click: null,
  params: [],
};

function payload(rows: TablePayload['rows']): TablePayload {
  return {
    chart: 'table',
    columns: [
      { key: 'name', title: '姓名' },
      { key: 'target', title: '月目标', format: 'wan' },
      { key: 'done', title: '已完成', format: 'wan' },
      { key: 'severity', title: '告警', format: 'severity' },
    ],
    rows,
  };
}

/** 表体（不含 tfoot 合计行）首列姓名序列。 */
function bodyNames(): string[] {
  const tbody = document.querySelector('.data-table tbody');
  return Array.from(tbody?.querySelectorAll('tr') ?? []).map(
    (tr) => tr.querySelector('td')?.textContent ?? '',
  );
}

function renderTable(rows: TablePayload['rows']) {
  const cube = cardToCube(CARD, payload(rows));
  render(<TableCard cube={cube} />);
}

describe('TableCard 列排序', () => {
  it('数值列三态：升序 → 降序 → 恢复后端原序，aria-sort 同步', () => {
    renderTable([
      { name: '甲', target: 240, done: 100, severity: 'ok' },
      { name: '乙', target: 240, done: 50, severity: 'p1' },
      { name: '丙', target: 240, done: 200, severity: 'p0' },
    ]);
    expect(bodyNames()).toEqual(['甲', '乙', '丙']);

    const head = screen.getByRole('button', { name: /已完成/ });
    fireEvent.click(head); // asc
    expect(bodyNames()).toEqual(['乙', '甲', '丙']);
    expect(head.closest('th')?.getAttribute('aria-sort')).toBe('ascending');

    fireEvent.click(head); // desc
    expect(bodyNames()).toEqual(['丙', '甲', '乙']);
    expect(head.closest('th')?.getAttribute('aria-sort')).toBe('descending');

    fireEvent.click(head); // 还原
    expect(bodyNames()).toEqual(['甲', '乙', '丙']);
    expect(head.closest('th')?.getAttribute('aria-sort')).toBe('none');
  });

  it('空值恒沉底：升序降序 null 都在最后', () => {
    renderTable([
      { name: '甲', target: 240, done: 100, severity: 'ok' },
      { name: '乙', target: 240, done: null, severity: 'ok' }, // done 空但 target 在 → 非 missing
      { name: '丙', target: 240, done: 50, severity: 'ok' },
    ]);
    const head = screen.getByRole('button', { name: /已完成/ });
    fireEvent.click(head); // asc
    expect(bodyNames()).toEqual(['丙', '甲', '乙']);
    fireEvent.click(head); // desc：空值仍沉底，不翻上来
    expect(bodyNames()).toEqual(['甲', '丙', '乙']);
  });

  it('severity 列按告警级别排（p0 最前），不是字符串序', () => {
    renderTable([
      { name: '甲', target: 240, done: 100, severity: 'ok' },
      { name: '乙', target: 240, done: 50, severity: 'p1' },
      { name: '丙', target: 240, done: 0, severity: 'p0' },
    ]);
    fireEvent.click(screen.getByRole('button', { name: /告警/ }));
    expect(bodyNames()).toEqual(['丙', '乙', '甲']);
  });

  it('derived.missing（不可算）行不参与排序恒沉底，合计行不受影响', () => {
    renderTable([
      { name: '甲', target: 240, done: 100, severity: 'ok' },
      { name: '丁', target: null, done: 30, severity: 'p2' }, // target 缺 → missing
      { name: '乙', target: 240, done: 50, severity: 'p1' },
    ]);
    const head = screen.getByRole('button', { name: /已完成/ });
    fireEvent.click(head); // asc：丁 done=30 本应最前，但 missing 沉底
    expect(bodyNames()).toEqual(['乙', '甲', '丁']);

    // 合计基于全量行（含 missing），与排序无关
    const tfoot = document.querySelector('.data-table tfoot');
    expect(tfoot?.textContent).toContain('合计');
    expect(tfoot?.textContent).toContain(formatCell(100 + 30 + 50, 'wan'));
  });
});
