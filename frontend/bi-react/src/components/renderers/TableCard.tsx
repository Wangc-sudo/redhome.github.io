/**
 * 表格卡：吸顶表头 + 缺失显示「—」+ 合计行 + ARIA + 行级告警 chip。
 *
 * 数据流：
 *   CardPayload.table → cardToCube(fromTable) → CubeSchema{chart:'table', columns, rows}
 *   → derived['rowKey'] = { target, done, shortfall, alert, ... }
 *
 * 列定义优先级：cube.columns（后端列序） > [...dimensions, ...measures]
 * 合计行：数值列求和，比率列不求和（完成率均值无意义）
 * 告警格：format='severity' 的列渲染成 AlertChip；首列无独立告警列时附加 chip
 */
import type { CubeSchema, CubeValue, DerivedMetric, FieldDef } from '../../types/cube';
import { formatCell } from '../../data/adapter/cardToCube';
import { alertOf } from '../../data/severity';
import { AlertChip } from './AlertChip';
// 列定义优先取 cube.columns（后端列序/格式），否则退化 dimensions+measures。
function columnsOf(cube: CubeSchema): FieldDef[] {
  if (cube.columns?.length) return cube.columns;
  return [...cube.dimensions, ...cube.measures];
}

function sumOf(cube: CubeSchema, col: FieldDef): number | null {
  // 比率不求和（合计完成率没有意义）
  if (col.type !== 'measure' || col.dataType === 'string' || col.dataType === 'percent') return null;
  let sum = 0;
  let any = false;
  for (const r of cube.rows) {
    const v = r[col.key];
    if (typeof v === 'number' && Number.isFinite(v)) {
      sum += v;
      any = true;
    }
  }
  return any ? sum : null;
}

/** 告警格：后端（或过渡派生层）已算好 severity，这里只查表出 chip，不算。 */
function SeverityCell({ value, derived }: { value: CubeValue; derived?: DerivedMetric }) {
  const alert = derived?.alert ?? alertOf(value);
  return alert ? <AlertChip alert={alert} /> : <>—</>;
}

/**
 * has_fact=false 诊断角标（「挂零」：本月截至今日无销单行）。
 * ---------------------------------------------------------------------------
 * 纯渲染、**不参与任何判定**：has_fact 是后端下发的诊断字段（docs/derived-metrics.md §5，
 * 裁决 #3：字段保留作诊断，不参与 severity），前端既不改告警颜色也不改数值，
 * 只在行上加一个中性的 .badge-defect 标记，把「挂零」这一数据事实显式化（CubeSchema §5）。
 */
function NoFactBadge() {
  return (
    <span
      className="badge-defect"
      title="本月截至今日无销单（后端 has_fact=false 诊断字段；仅提示，不影响告警判定与数值）"
    >
      挂零
    </span>
  );
}

export function TableCard({ cube }: { cube: CubeSchema }) {
  const columns = columnsOf(cube);
  if (columns.length === 0) return null;
  // 有独立告警列时，首列不再重复挂 chip
  const hasSeverityCol = columns.some((c) => c.format === 'severity');
  // 挂零角标挂在姓名列（anomaly_top 首列是 rank，角标跟着人走）；没有 name 列则退回首列
  const badgeColIdx = Math.max(0, columns.findIndex((c) => c.key === 'name'));

  return (
    <div className="card">
      <div className="card-title">{cube.title}</div>
      <div className="table-scroll">
        <table className="data-table" role="table">
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key} scope="col">
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {cube.rows.map((r, i) => {
              const rowKey = cube.rowKeys?.[i];
              const derived = rowKey ? cube.derived?.[rowKey] : undefined;
              return (
                <tr key={rowKey ?? i}>
                  {columns.map((c, ci) => (
                    <td key={c.key} className={ci === 0 ? '' : 'num'}>
                      {c.format === 'severity' ? (
                        <SeverityCell value={r[c.key]} derived={derived} />
                      ) : (
                        formatCell(r[c.key], c.format)
                      )}
                      {ci === 0 && !hasSeverityCol && derived?.alert && <AlertChip alert={derived.alert} />}
                      {ci === badgeColIdx && r['has_fact'] === false && <NoFactBadge />}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr>
              {columns.map((c, ci) => {
                if (ci === 0) return <td key={c.key}>合计</td>;
                const sum = sumOf(cube, c);
                return <td key={c.key}>{sum == null ? '—' : formatCell(sum, c.format)}</td>;
              })}
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}
