// @vitest-environment jsdom
/**
 * 筛选器空候选禁用态（前后端拉齐 V1，T6 裁决：占位页无候选值时禁用态）。
 * ---------------------------------------------------------------------------
 * 走 mock 后端（VITE_MOCK=1）：warehouses 源候选为空数组（占位页数据源未接入），
 * regions 源候选正常 —— 两种形态都钉死。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { FilterBar } from './FilterBar';

afterEach(cleanup);

describe('FilterBar · 空候选禁用态', () => {
  it('options 为空数组（占位页 warehouses）→ select 禁用 + title「暂无候选值」', async () => {
    render(
      <FilterBar
        filters={[{ param: 'warehouse', source: 'warehouses', label: '仓库' }]}
        value={{}}
        onChange={() => undefined}
      />,
    );

    const select = await screen.findByRole('combobox');
    await waitFor(() => expect(select).toHaveProperty('disabled', true), { timeout: 3000 });
    expect(select.getAttribute('title')).toBe('暂无候选值');
  });

  it('options 有候选（regions）→ select 可用、无禁用 title（防回归）', async () => {
    render(
      <FilterBar
        filters={[{ param: 'region', source: 'regions', label: '区域' }]}
        value={{}}
        onChange={() => undefined}
      />,
    );

    const select = await screen.findByRole('combobox');
    await waitFor(() => expect(screen.getByText('杭州')).toBeTruthy(), { timeout: 3000 });
    expect(select).toHaveProperty('disabled', false);
    expect(select.getAttribute('title')).toBeNull();
  });

  it('filters 为空 → 不渲染筛选条', () => {
    const { container } = render(<FilterBar filters={[]} value={{}} onChange={() => undefined} />);
    expect(container.firstChild).toBeNull();
  });
});
