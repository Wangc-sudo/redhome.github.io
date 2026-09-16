/**
 * 单个看板视图：取后端定义 → 生成栅格 → 每张卡独立取数。
 *
 * 数据流：
 *   /api/v1/dashboards/{id} → useDashboardDef → def.cards[]
 *   → GridItem[]（每张卡包装成 WidgetCard）
 *   → DashboardGrid（拖拽布局）
 *
 * 切看板即重置筛选/下钻（避免上一张板的 region 串到新板）
 * 卡片高度按 chart 给默认值（rowHeight=64），后端若补 min_h 再改成透传
 */
import { useEffect, useMemo } from 'react';
import { useDashboardDef } from '../../data/useDashboard';
import { useDrill } from '../../hooks/useDrill';
import type { ChartKind } from '../../data/types';
import { EmptyState, ErrorState, LoadingState } from '../common/States';
import { DashboardGrid, type GridItem } from './DashboardGrid';
import { FilterBar } from './FilterBar';
import { WidgetCard } from './WidgetCard';
// 卡片高度按 chart 给默认值（rowHeight=64），后端若补 min_h 再改成透传。
const HEIGHT: Record<ChartKind, number> = { scalar: 2, line: 5, bar: 5, pie: 5, table: 7 };

/** 刷新周期文案：T+1（86400s）按天显示，其余按小时/分钟。 */
function refreshText(seconds: number): string {
  if (seconds <= 0) return '不自动刷新';
  if (seconds % 86400 === 0) return `每 ${seconds / 86400} 天自动刷新`;
  if (seconds % 3600 === 0) return `每 ${seconds / 3600} 小时自动刷新`;
  return `每 ${Math.round(seconds / 60)} 分钟自动刷新`;
}

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
          {def.cards.length} 张卡片 · {refreshText(def.refresh_seconds)}
        </p>
      </header>

      <FilterBar filters={def.filters ?? []} value={filters} onChange={setFilter} />

      {items.length === 0 ? (
        <EmptyState label="该看板暂无卡片" />
      ) : (
        <DashboardGrid dashboardId={def.id} items={items} />
      )}

      {/* 数据口径标注：14 页共用这一处实现（bi-ui 既有 .table-footer 样式） */}
      <footer className="table-footer">数据口径 T+1</footer>
    </>
  );
}
