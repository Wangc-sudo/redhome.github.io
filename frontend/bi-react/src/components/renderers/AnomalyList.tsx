/**
 * 异常清单：缺口 TOP N（降序），直接回答「今天该跟进谁」。
 *
 * 数据流：
 *   后端 kpi_shortfall/anomaly_top 卡 → CubeSchema{derived.shortfall, derived.alert}
 *   → 本渲染器按 shortfall 降序取 topN
 *
 * 排序键 = derived.shortfall（CubeSchema §2.1 的绝对缺口），missing 不参与排序
 * ⚠️ 本渲染器只**读取**派生结果，不含任何缺口算式（算式在后端 derived.py）
 */
import type { CubeSchema } from '../../types/cube';
import { AlertChip } from './AlertChip';
interface Props {
  cube: CubeSchema;
  topN?: number;
}

export function AnomalyList({ cube, topN = 10 }: Props) {
  const rows = cube.rows
    .map((r, i) => ({ name: cube.rowKeys?.[i] ?? String(r[cube.dimensions[0]?.key ?? ''] ?? ''), d: cube.rowKeys?.[i] ? cube.derived?.[cube.rowKeys[i]] : undefined }))
    .filter((x) => x.d && !x.d.missing && x.d.shortfall != null && x.d.shortfall > 0)
    .sort((a, b) => (b.d?.shortfall ?? 0) - (a.d?.shortfall ?? 0))
    .slice(0, topN);

  if (rows.length === 0) {
    return (
      <div className="card">
        <div className="card-title">缺口 TOP {topN}</div>
        <div className="empty">当前无缺口记录</div>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="card-title">缺口 TOP {topN}</div>
      <div className="anomaly-list">
        {rows.map((x) => (
          <div key={x.name} className={`anomaly-card ${x.d?.alert?.severity ?? 'ok'}`}>
            <div>
              <div className="who">{x.name}</div>
              <div className="action">缺口 {(x.d?.shortfall ?? 0).toLocaleString('zh-CN')}</div>
            </div>
            <div style={{ marginLeft: 'auto' }}>{x.d?.alert && <AlertChip alert={x.d.alert} />}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
