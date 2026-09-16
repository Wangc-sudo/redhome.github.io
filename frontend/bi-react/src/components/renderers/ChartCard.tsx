/**
 * 图表卡（折线/柱状）：option 纯由 CubeSchema 结构推导，不写业务计算。
 *
 * 数据流：
 *   CardPayload.line/bar → cardToCube(fromLine/fromBar) → CubeSchema{chart, dimensions, measures, rows}
 *   → echarts option（xAxis=首维度，series=各 measure）
 *
 * 性能：大数据走 large:true + lttb 采样（VISUALIZATION §4）
 * 主题色：从 bi-ui tokens.css 读取 CSS 变量，不在 JS 里维护第二套 token
 */
import ReactECharts from 'echarts-for-react';
import type { CubeSchema } from '../../types/cube';
// 大数据走 large + sampling（bi-ui/VISUALIZATION.md §4）。

/** 从 bi-ui tokens.css 读取主题色，避免在 JS 里维护第二套 token。 */
function cssVar(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

export function chartPalette(): string[] {
  return [
    cssVar('--primary', '#2563eb'),
    cssVar('--secondary', '#218879'),
    cssVar('--severity-p2', '#9a6700'),
    cssVar('--info', '#0969da'),
    cssVar('--severity-p1', '#bc4c00'),
    cssVar('--severity-ok', '#1a7f37'),
  ];
}

function toNum(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

export function ChartCard({ cube }: { cube: CubeSchema }) {
  const dimKey = cube.dimensions[0]?.key;
  const categories = cube.rows.map((r) => String(dimKey ? (r[dimKey] ?? '') : ''));
  const series = cube.measures.map((m) => ({
    name: m.label,
    type: cube.chart === 'line' ? ('line' as const) : ('bar' as const),
    large: true,
    sampling: 'lttb' as const,
    data: cube.rows.map((r) => toNum(r[m.key])),
  }));

  const option = {
    color: chartPalette(),
    tooltip: { trigger: 'axis' },
    legend: { data: cube.measures.map((m) => m.label), top: 0 },
    grid: { left: 48, right: 16, top: 36, bottom: 24 },
    xAxis: { type: 'category', data: categories },
    yAxis: { type: 'value' },
    series,
  };

  return (
    <div className="card">
      <div className="card-title">{cube.title}</div>
      <div className="chart-large">
        <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge />
      </div>
    </div>
  );
}
