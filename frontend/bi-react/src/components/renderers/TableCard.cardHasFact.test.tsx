// @vitest-environment jsdom
/**
 * 卡级 has_fact（占位卡 = 应接入未接入）渲染（前后端拉齐 V1，2026-09-16）。
 * ---------------------------------------------------------------------------
 * 契约：占位卡 payload 顶层 `has_fact:false`（非行级），形态
 *   {"chart":"table","columns":[...],"rows":[],"has_fact":false,"unit":"元"}
 * 守四件事：
 *   ① has_fact=false → 卡级占位态：列头照常渲染 + 空态文案「暂无数据（待接入）」
 *      + 卡级 p0 chip（reason「应接入未接入」，severity.ts 查表）+「待接入」角标；
 *   ② 行级「挂零」角标不重复渲染（占位卡 rows 为空，行级逻辑零改动）；
 *   ③ has_fact 缺省 / true → 行为与现状逐位一致（防回归）；
 *   ④ 透传只搬运：cube.hasFact 原样等于 payload.has_fact，无判定逻辑。
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { cardToCube } from '../../data/adapter/cardToCube';
import type { CardDef, TablePayload } from '../../data/types';
import { TableCard } from './TableCard';

afterEach(cleanup);

const CARD: CardDef = {
  card: 'table_inventory_age',
  title: '库存与库龄',
  chart: 'table',
  span: 12,
  on_click: null,
  params: [],
};

/** 占位卡契约形态：结构一次到位（库龄分桶列），rows 空 + 顶层 has_fact:false */
function placeholderPayload(hasFact?: boolean): TablePayload {
  return {
    chart: 'table',
    unit: '元',
    ...(hasFact === undefined ? {} : { has_fact: hasFact }),
    columns: [
      { key: 'sku', title: 'SKU' },
      { key: 'qty', title: '库存量', format: 'int' },
      { key: 'age_0_90', title: '库龄0-90天', format: 'int' },
      { key: 'age_91_180', title: '库龄91-180天', format: 'int' },
      { key: 'age_181_365', title: '库龄181-365天', format: 'int' },
      { key: 'age_over_365', title: '库龄>365天', format: 'int' },
    ],
    rows: [],
  };
}

/** 正常卡（has_fact 缺省 / true 的防回归基准） */
function normalPayload(hasFact?: boolean): TablePayload {
  return {
    chart: 'table',
    unit: '元',
    ...(hasFact === undefined ? {} : { has_fact: hasFact }),
    columns: [
      { key: 'channel', title: '渠道' },
      { key: 'sales', title: '本月销售额', format: 'wan' },
      { key: 'stores', title: '店铺数', format: 'int' },
    ],
    rows: [{ channel: '直播', sales: 5619927.19, stores: 5 }],
  };
}

describe('卡级 has_fact=false：占位卡（应接入未接入）', () => {
  it('透传只搬运：cube.hasFact 原样等于 payload.has_fact（false→false / true→true / 缺省→不落字段）', () => {
    expect(cardToCube(CARD, placeholderPayload(false)).hasFact).toBe(false);
    expect(cardToCube(CARD, placeholderPayload(true)).hasFact).toBe(true);
    expect(cardToCube(CARD, placeholderPayload()).hasFact).toBeUndefined();
  });

  it('渲染卡级占位态：「待接入」角标 + p0 chip「应接入未接入」+ 空态文案，列头照常', () => {
    const cube = cardToCube(CARD, placeholderPayload(false));
    render(<TableCard cube={cube} />);

    // 「待接入」角标复用 bi-ui .badge-defect
    const badge = screen.getByText('待接入', { selector: 'span.badge-defect' });
    expect(badge.getAttribute('title')).toContain('has_fact');

    // 卡级 p0 chip（severity.ts 查表文案，不自造）
    const chip = screen.getByText('P0');
    expect(chip.closest('.alert-chip')?.className).toContain('p0');
    expect(screen.getByText('应接入未接入')).toBeTruthy();

    // 表体空态文案 + 列头照常渲染（结构一次到位）
    expect(screen.getByText(/暂无数据（待接入）/)).toBeTruthy();
    for (const h of ['SKU', '库存量', '库龄0-90天', '库龄91-180天', '库龄181-365天', '库龄>365天']) {
      expect(screen.getByText(h)).toBeTruthy();
    }

    // 行级「挂零」角标不重复渲染（占位卡无数据行）
    expect(screen.queryByText('挂零')).toBeNull();
    // 占位态不出合计行（没有可合计的数据）
    expect(screen.queryByText('合计')).toBeNull();
  });

  it('一张卡只占位一次：占位态下 P0 chip 与「待接入」各出现一次', () => {
    const cube = cardToCube(CARD, placeholderPayload(false));
    render(<TableCard cube={cube} />);
    expect(screen.getAllByText('P0')).toHaveLength(1);
    expect(screen.getAllByText('应接入未接入')).toHaveLength(1);
    expect(screen.getAllByText('待接入', { selector: 'span.badge-defect' })).toHaveLength(1);
  });
});

describe('has_fact 缺省 / true：行为与现状逐位一致（防回归）', () => {
  it.each([{ label: '缺省', payload: normalPayload() }, { label: 'true', payload: normalPayload(true) }])(
    '$label：正常渲染数据行 + 合计行，不出现任何占位态元素',
    ({ payload }) => {
      const cube = cardToCube(CARD, payload);
      render(<TableCard cube={cube} />);

      // 数据行与合计行照常
      expect(screen.getByText('直播')).toBeTruthy();
      expect(screen.getByText('合计')).toBeTruthy();
      // 占位态元素一律不出现
      expect(screen.queryByText('待接入', { selector: 'span.badge-defect' })).toBeNull();
      expect(screen.queryByText(/暂无数据（待接入）/)).toBeNull();
      expect(screen.queryByText('应接入未接入')).toBeNull();
      expect(screen.queryByText('P0')).toBeNull();
    },
  );
});
