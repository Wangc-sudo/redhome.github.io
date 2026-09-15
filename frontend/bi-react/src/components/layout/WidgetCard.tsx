import { cardKey, refreshResource, useCardCube } from '../../data/useCube';
import { pickParams } from '../../data/biWebClient';
import type { CardDef } from '../../data/types';
import { EmptyState, ErrorState, LoadingState } from '../common/States';
import { ScalarCard } from '../renderers/ScalarCard';
import { ChartCard } from '../renderers/ChartCard';
import { TableCard } from '../renderers/TableCard';
import { PieCard } from '../renderers/PieCard';

// 单卡组件：自己订阅共享缓存（Rule of Hooks 合规），按 chart 选哑渲染器。
// 取代旧的 renderWidget()（在 DashboardGrid 的 map 里直接调 hook，N 卡状态串台）。
interface Props {
  dashboardId: string;
  card: CardDef;
  params: Record<string, string>;
  refreshMs: number;
}

export function WidgetCard({ dashboardId, card, params, refreshMs }: Props) {
  const { data, loading, error } = useCardCube(dashboardId, card, params, refreshMs);

  if (!data) {
    if (loading) return <LoadingState />;
    if (error) {
      return (
        <ErrorState
          error={error}
          onRetry={() => refreshResource(cardKey(dashboardId, card.card, pickParams(card, params)))}
        />
      );
    }
    return <EmptyState />;
  }

  if (data.rows.length === 0 && data.chart !== 'scalar') return <EmptyState />;

  switch (data.chart) {
    case 'scalar':
      return <ScalarCard cube={data} />;
    case 'line':
    case 'bar':
      return <ChartCard cube={data} />;
    case 'pie':
      return <PieCard cube={data} />;
    case 'table':
    default:
      return <TableCard cube={data} />;
  }
}
