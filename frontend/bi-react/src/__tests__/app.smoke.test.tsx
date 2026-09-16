// @vitest-environment jsdom
/**
 * 端到端冒烟：Mock 后端 → App 挂载 → 看板卡片渲染成 DOM。
 * 目的不是快照 UI，而是守住几条铁律不会被改坏：
 *   - 在 map 里调 hook 的写法一旦回来，这里会立刻「hooks 数量不一致」报错
 *   - 渲染器读不到 CubeSchema（契约适配断了）时，这里会渲染成空板
 * echarts / react-grid-layout 在 jsdom 里没有意义，直接替身。
 */
import React from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="chart" />,
}));

vi.mock('react-grid-layout', () => ({
  __esModule: true,
  default: ({ children }: { children: React.ReactNode }) => <div data-testid="grid">{children}</div>,
  WidthProvider: (C: React.ComponentType<Record<string, unknown>>) => C,
}));

beforeAll(() => {
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
});

async function mountAt(hash: string) {
  window.location.hash = hash;
  const { default: App } = await import('../App');
  return render(<App />);
}

describe('App 冒烟（mock 后端）', () => {
  it('l1-cockpit：渲染出卡片标题 + KPI 数值（说明契约适配链路通）', async () => {
    await mountAt('#/d/l1-cockpit');

    await waitFor(() => expect(screen.getByText('首页驾驶舱')).toBeTruthy(), { timeout: 3000 });
    // 后端卡片标题
    expect(screen.getByText('线下本月累计销售')).toBeTruthy();
    expect(screen.getByText('渠道本月对比')).toBeTruthy();
    // KPI：原始值 4,534,023 元 → 缩放后 453.4 万
    await waitFor(() => expect(screen.getAllByText(/453\.4/).length).toBeGreaterThan(0), { timeout: 3000 });
  });

  it('l1-cockpit：后端派生的两张卡渲染出告警 chip（severity 原样透传，不重算）', async () => {
    await mountAt('#/d/l1-cockpit');

    await waitFor(() => expect(screen.getByText('目标缺口与告警')).toBeTruthy(), { timeout: 3000 });
    await waitFor(() => expect(screen.getByText('今日跟进（缺口 TOP10）')).toBeTruthy(), { timeout: 3000 });

    // 后端给的是 p1（真机 payload），前端不得改判；chip 必须带文字
    await waitFor(() => expect(screen.getAllByText('P1').length).toBeGreaterThan(0), { timeout: 3000 });
    expect(screen.getAllByText('缺口超半月产能，本周内处理').length).toBeGreaterThan(0);
    // 名次列（anomaly_top 首列）
    expect(screen.getByText('名次')).toBeTruthy();
  });

  it('l2-people：表格渲染；该卡后端不下发 severity → 前端不得自造告警 chip', async () => {
    await mountAt('#/d/l2-people');

    await waitFor(() => expect(screen.getByText('人员销售榜')).toBeTruthy(), { timeout: 3000 });
    await waitFor(() => expect(screen.getByText('张伟')).toBeTruthy(), { timeout: 3000 });

    // 后端 table_people_leaderboard 没有 severity 列；前端已无任何派生/告警实现
    // （derive.ts 已整文件删除），所以这里**不能**出现任何告警 chip —— 出现即说明
    // 有人把前端判定加回来了。
    expect(screen.queryAllByText(/今日跟进|缺口超半月产能|有缺口，持续观察|P0|P1|P2/)).toHaveLength(0);
  });

  it('导航扩到 14 页：占位页集中在尾部且标题带「（待接入）」', async () => {
    await mountAt('#/d/l1-cockpit');
    await waitFor(() => expect(screen.getByText('首页驾驶舱')).toBeTruthy(), { timeout: 3000 });
    await waitFor(() => expect(screen.getByText('合同核销（待接入）')).toBeTruthy(), { timeout: 3000 });
    // 14 页导航项齐全（5 已上线 + 资金安全 + 3 月报 + 5 占位）
    const links = document.querySelectorAll('.sidebar nav a');
    expect(links).toHaveLength(14);
  });

  it('l2-inventory（占位页）：渲染「待接入」+ 卡级 p0 chip「应接入未接入」+ footer「数据口径 T+1」', async () => {
    await mountAt('#/d/l2-inventory');

    // 标题与侧边栏链接同名，锁定 h1
    await waitFor(() => expect(screen.getByRole('heading', { name: '库存与库龄（待接入）' })).toBeTruthy(), { timeout: 3000 });
    await waitFor(() => expect(screen.getByText(/暂无数据（待接入）/)).toBeTruthy(), { timeout: 3000 });

    // 「待接入」角标（卡片 badge-defect）+ 卡级 p0 chip（severity.ts 查表文案）
    expect(screen.getByText('待接入', { selector: 'span.badge-defect' })).toBeTruthy();
    expect(screen.getByText('应接入未接入')).toBeTruthy();
    // 列头照常渲染（结构一次到位：库龄分桶，非效期口径）
    expect(screen.getByText('库龄0-90天')).toBeTruthy();
    expect(screen.getByText('库龄>365天')).toBeTruthy();
    // 行级「挂零」角标不误渲染（占位卡无数据行）
    expect(screen.queryByText('挂零')).toBeNull();
    // 页尾统一口径标注（14 页共用 DashboardView 一处实现）
    expect(screen.getByText('数据口径 T+1')).toBeTruthy();
  });
});
