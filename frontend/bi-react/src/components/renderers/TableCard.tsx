import type { CubeSchema, CubeValue, DerivedMetric, FieldDef } from '../../types/cube';
import { formatCell } from '../../data/adapter/cardToCube';
import { alertOf } from '../../data/severity';
import { AlertChip } from './AlertChip';

// 表格卡：吸顶表头 + 缺失显示「—」+ 合计行 + ARIA + 行级告警 chip。
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

export function TableCard({ cube }: { cube: CubeSchema }) {
  const columns = columnsOf(cube);
  if (columns.length === 0) return null;
  // 有独立告警列时，首列不再重复挂 chip
  const hasSeverityCol = columns.some((c) => c.format === 'severity');

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
