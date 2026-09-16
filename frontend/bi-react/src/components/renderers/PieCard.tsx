/**
 * 饼/环图：吃 (name, value) 标准结构，扇区 = 维度取值。
 *
 * 数据流：
 *   CardPayload.pie → cardToCube(fromPie) → CubeSchema{chart:'pie', rows:[{name,value}]}
 *
 * 长名称处理：走 tooltip 而非 label（SKU 名可能很长）
 * 图例：在下方横向滚动排布（避免右侧图例挤爆卡片）
 */
import ReactECharts from 'echarts-for-react';
import type { CubeSchema } from '../../types/cube';
import { chartPalette } from './ChartCard';
// 长名称走 tooltip，图例在下方横向排布（SKU 名很长，放右侧会挤爆卡片）。
export function PieCard({ cube }: { cube: CubeSchema }) {
  const data = cube.rows.map((r) => ({
    name: String(r['name'] ?? ''),
    value: typeof r['value'] === 'number' ? r['value'] : 0,
  }));

  const option = {
    color: chartPalette(),
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { type: 'scroll', bottom: 0 },
    series: [
      {
        type: 'pie',
        radius: ['45%', '70%'],
        center: ['50%', '45%'],
        avoidLabelOverlap: true,
        label: { show: false },
        data,
      },
    ],
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
