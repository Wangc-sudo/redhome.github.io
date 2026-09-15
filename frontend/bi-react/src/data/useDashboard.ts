/**
 * 看板级取数：列表 / 定义 / 筛选项。
 * 全部走 usePolled 共享缓存（同一 key 只发一次请求），组件禁止直接 fetch。
 */

import { useCallback } from 'react';
import { fetchDashboardDef, fetchDashboards, fetchOptions } from './biWebClient';
import { paramsKey, usePolled, type ResourceState } from './useCube';
import type { DashboardDefinition, DashboardSummary } from './types';

export function useDashboardList(refreshMs = 0): ResourceState<DashboardSummary[]> {
  const loader = useCallback(async (signal: AbortSignal) => await fetchDashboards(signal), []);
  return usePolled<DashboardSummary[]>('dashboards', loader, refreshMs);
}

export function useDashboardDef(id: string | undefined): ResourceState<DashboardDefinition> {
  const loader = useCallback(
    async (signal: AbortSignal) => {
      if (!id) throw new Error('缺少看板 id');
      return await fetchDashboardDef(id, signal);
    },
    [id],
  );
  return usePolled<DashboardDefinition>(id ? `dashboard:${id}` : null, loader, 0);
}

export function useFilterOptions(source: string | undefined): ResourceState<string[]> {
  const loader = useCallback(
    async (signal: AbortSignal) => {
      if (!source) throw new Error('缺少选项源');
      return await fetchOptions(source, signal);
    },
    [source],
  );
  return usePolled<string[]>(source ? `options:${source}` : null, loader, 0);
}

export { paramsKey };
