import { useEffect, useMemo } from 'react';
import { useDashboardDef } from '../../data/useDashboard';
import { useDrill } from '../../hooks/useDrill';
import type { ChartKind } from '../../data/types';
import { EmptyState, ErrorState, LoadingState } from '../common/States';
import { DashboardGrid, type GridItem } from './DashboardGrid';
import { FilterBar } from './FilterBar';
import { WidgetCard } from './WidgetCard';

// 单个看板视图：取后端定义 → 生成栅格 → 每张卡独立取数。
// 卡片高度按 chart 给默认值（rowHeight=64），后端若补 min_h 再改成透传。
const HEIGHT: Record<ChartKind, number> = { scalar: 2, line: 5, bar: 5, pie: 5, table: 7 };

export function DashboardView({ id }: { id: string | undefined }) {
  const { data: def, loading, error } = useDashboardDef(id);
  const filters = useDrill((s) => s.filters);
  const setFilter = useDrill((s) => s.setFilter);
  const setPath = useDrill((s) => s.setPath);

  // 切看板即重置筛选/下钻（避免上一张板的 region 串到新板）
  useEffect(() => {
    if (id) setPath(id);
  }, [id, setPath]);

  const refreshMs = (def?.refresh_seconds ?? 0) * 1000;

  const items = useMemo<GridItem[]>(() => {
    if (!def || !id) return [];
    return def.cards.map((c) => ({
      i: c.card,
      span: c.span,
      h: HEIGHT[c.chart] ?? 5,
      node: <WidgetCard dashboardId={id} card={c} params={filters} refreshMs={refreshMs} />,
    }));
  }, [def, id, filters, refreshMs]);

  if (!id) return <EmptyState label="未选择看板" />;
  if (!def && loading) return <LoadingState />;
  if (!def && error) return <ErrorState error={error} />;
  if (!def) return <EmptyState />;

  return (
    <>
      <header className="page-head">
        <h1>{def.title}</h1>
        <p>
          {def.cards.length} 张卡片 · {def.refresh_seconds > 0 ? `每 ${Math.round(def.refresh_seconds / 60)} 分钟自动刷新` : '不自动刷新'}
        </p>
      </header>

      <FilterBar filters={def.filters ?? []} value={filters} onChange={setFilter} />

      {items.length === 0 ? (
        <EmptyState label="该看板暂无卡片" />
      ) : (
        <DashboardGrid dashboardId={def.id} items={items} />
      )}
    </>
  );
}
