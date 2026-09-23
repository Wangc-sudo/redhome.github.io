/**
 * 表格卡：吸顶表头 + 缺失显示「—」+ 合计行 + ARIA + 行级告警 chip + 列排序。
 *
 * 数据流：
 *   CardPayload.table → cardToCube(fromTable) → CubeSchema{chart:'table', columns, rows}
 *   → derived['rowKey'] = { target, done, shortfall, alert, ... }
 *
 * 列定义优先级：cube.columns（后端列序） > [...dimensions, ...measures]
 * 合计行：数值列求和，比率列不求和（完成率均值无意义）；固定在 tfoot，不参与排序
 * 告警格：format='severity' 的列渲染成 AlertChip；首列无独立告警列时附加 chip
 * 排序：表头点击三态循环（升序→降序→恢复后端原序）；
 *   空值与 derived.missing 行恒沉底（missing 语义 = 不可算，不参与排序）；
 *   severity 列按 p0>p1>p2>ok 级别排，其余列数值优先、退回 zh-CN 字典序。
 */
import { useMemo, useState } from 'react';
import type { CubeSchema, CubeValue, DerivedMetric, FieldDef } from '../../types/cube';
import { formatCell } from '../../data/adapter/cardToCube';
import { alertOf, noFactCardAlert } from '../../data/severity';
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

/* ---------------------------------------------------------------------- 排序 */

type SortDir = 'asc' | 'desc';
interface SortState {
  key: string;
  dir: SortDir;
}

/** severity 级别序：p0 最严重排最前（升序 = 先出最该看的）。 */
const SEVERITY_RANK: Record<string, number> = { p0: 0, p1: 1, p2: 2, ok: 3 };

function severityRank(v: CubeValue): number {
  return SEVERITY_RANK[String(v)] ?? Number.MAX_SAFE_INTEGER;
}

/** 空值（null/''/boolean 诊断字段）不可比，恒沉底——升序降序都在最后。 */
function isBlank(v: CubeValue): boolean {
  return v == null || v === '' || typeof v === 'boolean';
}

function compareCell(a: CubeValue, b: CubeValue): number {
  const numA = typeof a === 'number' ? a : Number(a);
  const numB = typeof b === 'number' ? b : Number(b);
  if (Number.isFinite(numA) && Number.isFinite(numB)) return numA - numB;
  return String(a).localeCompare(String(b), 'zh-CN');
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
  const [sort, setSort] = useState<SortState | null>(null);

  const columns = columnsOf(cube);

  // 排序后的行下标序列：rows 与 rowKeys 按下标对齐重排，derived 挂载关系不变。
  // 未排序时 = 后端原序（恒等映射），零成本。
  const order = useMemo(() => {
    const idx = cube.rows.map((_, i) => i);
    if (!sort) return idx;
    const col = columns.find((c) => c.key === sort.key);
    if (!col) return idx;
    const isSeverity = col.format === 'severity';
    // derived.missing = 不可算（CubeSchema：显示 — 且不参与排序）→ 恒沉底
    const isMissing = (i: number) => {
      const key = cube.rowKeys?.[i];
      return key ? (cube.derived?.[key]?.missing ?? false) : false;
    };
    const cmp = (ia: number, ib: number) => {
      const va = cube.rows[ia][sort.key];
      const vb = cube.rows[ib][sort.key];
      const blankA = isBlank(va);
      const blankB = isBlank(vb);
      if (blankA && blankB) return 0;
      if (blankA !== blankB) return blankA ? 1 : -1; // 空值恒沉底，与方向无关
      const base = isSeverity ? severityRank(va) - severityRank(vb) : compareCell(va, vb);
      return sort.dir === 'asc' ? base : -base;
    };
    const sortable = idx.filter((i) => !isMissing(i)).sort(cmp);
    return [...sortable, ...idx.filter(isMissing)];
    // columns 由 cube 派生，随 cube 一起变；列入依赖避免闭包陈旧
  }, [cube, columns, sort]);

  if (columns.length === 0) return null;
  // 有独立告警列时，首列不再重复挂 chip
  const hasSeverityCol = columns.some((c) => c.format === 'severity');
  // 挂零角标挂在姓名列（anomaly_top 首列是 rank，角标跟着人走）；没有 name 列则退回首列
  const badgeColIdx = Math.max(0, columns.findIndex((c) => c.key === 'name'));
  // 卡级挂零（cube.hasFact === false）：占位卡 = 应接入未接入。
  // 列头照常渲染（结构一次到位），表体渲染空态文案 + 卡级 p0 chip + 「待接入」角标；
  // 行级 has_fact 角标行为不变（占位卡 rows 本为空，不会重复渲染）。
  const cardNoFact = cube.hasFact === false;

  const toggleSort = (key: string) => {
    setSort((prev) => {
      if (prev?.key !== key) return { key, dir: 'asc' };
      if (prev.dir === 'asc') return { key, dir: 'desc' };
      return null; // 第三击恢复后端原序
    });
  };

  const ariaSortOf = (key: string) =>
    sort?.key !== key ? 'none' : sort.dir === 'asc' ? 'ascending' : 'descending';

  return (
    <div className="card">
      <div className="card-title">
        {cube.title}
        {cardNoFact && (
          <span
            className="badge-defect"
            title="该卡片应接入数据源、暂未接入（后端卡级 has_fact=false；挂零 = p0）"
          >
            待接入
          </span>
        )}
      </div>
      <div className="table-scroll">
        <table className="data-table" role="table">
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key} scope="col" aria-sort={ariaSortOf(c.key)}>
                  <button
                    type="button"
                    className={`th-sort${sort?.key === c.key ? ' is-sorted' : ''}`}
                    onClick={() => toggleSort(c.key)}
                    title={`按「${c.label}」排序`}
                  >
                    {c.label}
                    <span className="sort-ind" aria-hidden="true">
                      {sort?.key !== c.key ? '⇅' : sort.dir === 'asc' ? '↑' : '↓'}
                    </span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {cardNoFact ? (
              <tr>
                <td colSpan={columns.length} className="empty-cell">
                  暂无数据（待接入） <AlertChip alert={noFactCardAlert()} />
                </td>
              </tr>
            ) : (
              order.map((i) => {
                const r = cube.rows[i];
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
              })
            )}
          </tbody>
          {!cardNoFact && (
            <tfoot>
              <tr>
                {columns.map((c, ci) => {
                  if (ci === 0) return <td key={c.key}>合计</td>;
                  const sum = sumOf(cube, c);
                  return <td key={c.key}>{sum == null ? '—' : formatCell(sum, c.format)}</td>;
                })}
              </tr>
            </tfoot>
          )}
        </table>
      </div>
    </div>
  );
}
