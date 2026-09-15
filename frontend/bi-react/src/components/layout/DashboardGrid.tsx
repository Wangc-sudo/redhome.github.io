import { useMemo, useRef, useState, type ReactNode } from 'react';
import GridLayout, { WidthProvider, type Layout } from 'react-grid-layout';
import { ErrorBoundary } from '../common/ErrorBoundary';

// 布局层：react-grid-layout 拖动/缩放。
// 布局源 = 后端 cards[].span（12 栅格），用户拖拽后写 localStorage；
// 初始化时**读取** localStorage 并与默认布局 merge（旧实现只写不读，刷新必回默认）。

const Grid = WidthProvider(GridLayout);

export interface GridItem {
  i: string;
  span: number;
  h: number;
  node: ReactNode;
}

const LAYOUT_PREFIX = 'bi-react:layout:';

function storageKeyOf(dashboardId: string): string {
  return `${LAYOUT_PREFIX}${dashboardId}`;
}

/** 按 span 在 12 栅格里顺序排布，超出换行。 */
function baseLayout(items: GridItem[]): Layout[] {
  const out: Layout[] = [];
  let x = 0;
  let y = 0;
  let rowH = 0;

  for (const it of items) {
    const w = Math.min(Math.max(Math.round(it.span) || 12, 1), 12);
    if (x + w > 12) {
      x = 0;
      y += rowH;
      rowH = 0;
    }
    out.push({ i: it.i, x, y, w, h: it.h, minW: 2, minH: 2 });
    x += w;
    rowH = Math.max(rowH, it.h);
  }
  return out;
}

function readStored(dashboardId: string): Layout[] {
  try {
    const raw = localStorage.getItem(storageKeyOf(dashboardId));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Layout[]) : [];
  } catch {
    return []; // 脏数据/隐私模式：静默退回默认布局
  }
}

/** 默认布局为骨架，仅覆盖用户真正拖过的卡（按 i 对齐），避免旧布局残留脏项。 */
function mergeLayout(base: Layout[], stored: Layout[]): Layout[] {
  if (stored.length === 0) return base;
  const byI = new Map(stored.map((l) => [l.i, l]));
  return base.map((b) => {
    const s = byI.get(b.i);
    if (!s) return b;
    return { ...b, x: s.x, y: s.y, w: s.w, h: s.h };
  });
}

interface Props {
  dashboardId: string;
  items: GridItem[];
}

export function DashboardGrid({ dashboardId, items }: Props) {
  const itemsRef = useRef(items);
  itemsRef.current = items;

  const [rev, setRev] = useState(0);
  const signature = items.map((it) => `${it.i}:${it.span}:${it.h}`).join('|');

  const layout = useMemo(
    () => mergeLayout(baseLayout(itemsRef.current), readStored(dashboardId)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dashboardId, signature, rev],
  );

  const onLayoutChange = (next: Layout[]) => {
    try {
      localStorage.setItem(storageKeyOf(dashboardId), JSON.stringify(next));
    } catch {
      /* 隐私模式写不了就算了，不影响使用 */
    }
  };

  const resetLayout = () => {
    try {
      localStorage.removeItem(storageKeyOf(dashboardId));
    } catch {
      /* ignore */
    }
    setRev((r) => r + 1);
  };

  return (
    <>
      <div className="filters">
        <span style={{ marginLeft: 'auto' }}>
          <button className="btn" onClick={resetLayout}>
            恢复默认布局
          </button>
        </span>
      </div>
      <Grid
        className="dashboard-grid"
        layout={layout}
        cols={12}
        rowHeight={64}
        margin={[16, 16]}
        draggableHandle=".widget__drag"
        onLayoutChange={onLayoutChange}
      >
        {items.map((it) => (
          <div key={it.i} className="widget">
            <div className="widget__head">
              <span className="widget__drag" title="拖动排序">
                ⠿
              </span>
            </div>
            <ErrorBoundary name={it.i}>{it.node}</ErrorBoundary>
          </div>
        ))}
      </Grid>
    </>
  );
}
