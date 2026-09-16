/**
 * 契约适配层：后端卡片 payload → CubeSchema
 * ---------------------------------------------------------------------------
 * 后端给的是「按 chart 分派的可视化 payload」（scalar/line/bar/table/pie），
 * 前端组件只吃 CubeSchema（见 bi-ui/CubeSchema.md）。本文件是**唯一的转换点**：
 * 渲染器永远看不到 CardPayload，也就不会被后端字段调整波及。
 *
 * 归一约定：
 *   scalar → 单行 rows[0] + derived（target/rate/delta_pct/trend7）
 *   line   → rows = 每个日期一行，每条 series 一个 measure
 *   bar    → rows = 每个类目一行，(category, value)
 *   table  → rows + columns 直接透传（列序/格式以后端为准）
 *   pie    → rows = 每个扇区一行，(name, value)
 */

import type {
  BarPayload,
  CardDef,
  CardPayload,
  LinePayload,
  PiePayload,
  ScalarPayload,
  TablePayload,
} from '../types';
import type { CubeRow, CubeSchema, DataType, DerivedMetric, FieldDef, Severity } from '../../types/cube';
import { alertOf } from '../severity';

export interface CardToCubeOptions {
  dashboardId?: string;
  asOf?: string;
  /** 单个测试可覆盖「今天」，便于断言 severity 边界 */
  now?: Date;
}

const CHART_FALLBACK: CubeSchema['chart'] = 'table';

function dim(key: string, label: string): FieldDef {
  return { key, label, type: 'dimension', dataType: 'string' };
}

function measure(key: string, label: string, dataType: DataType, unit?: string | null): FieldDef {
  return { key, label, type: 'measure', dataType, unit: unit ?? undefined };
}

function toNumber(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v);
  return null;
}

/** 后端 format → CubeSchema dataType（只影响展示，不改语义） */
function dataTypeOfFormat(format?: string | null): DataType {
  switch (format) {
    case 'wan':
      return 'currency';
    case 'ratio':
    case 'pct':
    case 'percent':
      return 'percent';
    case 'int':
      return 'number';
    default:
      return 'string';
  }
}

/** 后端 format 里可参与「合计」的数值列 */
const NUMERIC_FORMATS = new Set(['wan', 'ratio', 'int', 'pct', 'percent']);

/** 空 payload / 未知 chart 的兜底：给一个「空表」CubeSchema，让渲染器走 EmptyState。 */
function emptyCube(card: CardDef, opts: CardToCubeOptions): CubeSchema {
  return {
    id: card.card,
    title: card.title,
    chart: CHART_FALLBACK,
    dimensions: [dim('name', '名称')],
    measures: [measure('value', card.title, 'number')],
    rows: [],
    rowKeys: [],
    asOf: opts.asOf,
    updatedAt: new Date().toISOString(),
  };
}

/* ------------------------------------------------------------------ 各卡分派 */

function fromScalar(card: CardDef, p: ScalarPayload, opts: CardToCubeOptions): CubeSchema {
  const unit = p.unit ?? null;
  const row: CubeRow = { metric: card.title, value: p.value };
  if (p.target != null) row.target = p.target;
  if (p.rate != null) row.rate = p.rate;

  const key = card.card;
  const derivedMetric = {
    target: p.target ?? undefined,
    // 有 target 时把 value 视作 done（只做字段搬运；缺口/告警一律后端算，前端不补）
    done: p.target != null ? (p.value ?? undefined) : undefined,
    progressRate: p.rate ?? undefined,
    delta: p.delta_pct ?? undefined,
    missing: p.value == null,
  };

  const cube: CubeSchema = {
    id: card.card,
    title: card.title,
    chart: 'scalar',
    unit,
    dimensions: [dim('metric', '指标')],
    measures: [measure('value', card.title, 'number', unit)],
    rows: [row],
    rowKeys: [key],
    derived: { [key]: derivedMetric },
    asOf: p.date ?? opts.asOf,
    updatedAt: new Date().toISOString(),
  };
  // 日环比卡：trend7 直接落到 rows 之外的补充序列，渲染器可选消费
  if (p.trend7?.length) {
    cube.derived![key] = { ...derivedMetric, delta: p.delta_pct ?? undefined };
  }
  return cube;
}

function fromLine(card: CardDef, p: LinePayload, opts: CardToCubeOptions): CubeSchema {
  const dates = p.dates ?? [];
  const series = p.series ?? [];
  const rows: CubeRow[] = dates.map((date, i) => {
    const row: CubeRow = { date };
    for (const s of series) row[s.name] = s.data?.[i] ?? null;
    return row;
  });

  return {
    id: card.card,
    title: card.title,
    chart: 'line',
    unit: p.unit ?? null,
    dimensions: [dim('date', '日期')],
    measures: series.map((s) => measure(s.name, s.name, 'number', p.unit)),
    rows,
    rowKeys: dates.map(String),
    asOf: opts.asOf,
    updatedAt: new Date().toISOString(),
  };
}

function fromBar(card: CardDef, p: BarPayload, opts: CardToCubeOptions): CubeSchema {
  const categories = p.categories ?? [];
  const values = p.values ?? [];
  const rows: CubeRow[] = categories.map((c, i) => ({
    category: c,
    value: values[i] ?? null,
  }));

  return {
    id: card.card,
    title: card.title,
    chart: 'bar',
    unit: p.unit ?? null,
    dimensions: [dim('category', card.title)],
    measures: [measure('value', card.title, 'number', p.unit)],
    rows,
    rowKeys: categories.map(String),
    asOf: opts.asOf,
    updatedAt: new Date().toISOString(),
  };
}

/**
 * 后端**已算好**的派生指标（kpi_shortfall / anomaly_top：shortfall/rate/
 * required_daily/severity 全在行里）→ DerivedMetric。
 *
 * ⚠️ 这里只做字段搬运，**不重算**：原样透传后端值，severity 仅查表装饰成
 * alert-chip。前端已无任何派生实现（derive.ts 已按路线图 P3 删除），
 * 因此不会出现「后端给 p2、前端再算成 p0」的口径打架。
 */
function backendDerivedOf(row: CubeRow): DerivedMetric | null {
  if (row['severity'] == null) return null; // 后端没给 severity = 该行无派生，前端不补（派生一律后端算）
  const target = toNumber(row['target']);
  const done = toNumber(row['done']);
  return {
    target: target ?? undefined,
    done: done ?? undefined,
    shortfall: toNumber(row['shortfall']) ?? undefined,
    requiredDaily: toNumber(row['required_daily']) ?? undefined,
    progressRate: toNumber(row['rate']) ?? undefined,
    // 不可算（target/done 缺失）显式标注，渲染「—」且不参与排序
    missing: target == null || done == null,
    alert: alertOf(row['severity']),
  };
}

function fromTable(card: CardDef, p: TablePayload, opts: CardToCubeOptions): CubeSchema {
  const columns = p.columns ?? [];
  const rows = (p.rows ?? []).map((r) => ({ ...r }) as CubeRow);
  // 行主键优先取 name 列（anomaly_top 首列是 rank，不能拿名次当主键）
  const keyCol = columns.find((c) => c.key === 'name') ?? columns[0];

  const fields: FieldDef[] = columns.map((c) => {
    const isKey = c.key === keyCol?.key;
    return {
      key: c.key,
      label: c.title,
      type: !isKey && NUMERIC_FORMATS.has(c.format ?? '') ? 'measure' : 'dimension',
      dataType: isKey ? 'string' : dataTypeOfFormat(c.format),
      format: c.format ?? undefined,
      // 后端派生的列（缺口/所需日均/告警）打标，渲染器据此只读不补算式
      derived: c.format === 'severity' || c.key === 'shortfall' || c.key === 'required_daily' ? true : undefined,
    };
  });

  const rowKeys = rows.map((r) => String((keyCol ? r[keyCol.key] : '') ?? ''));

  const derived: Record<string, DerivedMetric> = {};
  rows.forEach((r, i) => {
    const key = rowKeys[i];
    if (!key) return;
    const d = backendDerivedOf(r);
    if (d) derived[key] = d;
  });

  return {
    id: card.card,
    title: card.title,
    chart: 'table',
    unit: p.unit ?? null,
    dimensions: fields.filter((f) => f.type === 'dimension'),
    measures: fields.filter((f) => f.type === 'measure'),
    columns: fields,
    rows,
    rowKeys,
    derived: Object.keys(derived).length > 0 ? derived : undefined,
    asOf: p.as_of ?? opts.asOf,
    updatedAt: new Date().toISOString(),
    // 卡级 has_fact 顶层透传（占位卡 = false）；只搬运，零判定
    hasFact: p.has_fact ?? undefined,
  };
}

function fromPie(card: CardDef, p: PiePayload, opts: CardToCubeOptions): CubeSchema {
  const items = p.items ?? [];
  const rows: CubeRow[] = items.map((it) => ({ name: it.name, value: it.value }));

  return {
    id: card.card,
    title: card.title,
    chart: 'pie',
    unit: p.unit ?? null,
    dimensions: [dim('name', '名称')],
    measures: [measure('value', card.title, 'number', p.unit)],
    rows,
    rowKeys: items.map((it) => String(it.name)),
    asOf: opts.asOf,
    updatedAt: new Date().toISOString(),
  };
}

/* ---------------------------------------------------------------------- 出口 */

/** 卡片 payload → CubeSchema（纯归一 + 后端派生字段透传，前端零算式）。 */
export function cardToCube(
  card: CardDef,
  payload: CardPayload | null | undefined,
  opts: CardToCubeOptions = {},
): CubeSchema {
  if (!payload) return emptyCube(card, opts);

  const chart = (payload.chart ?? card.chart ?? CHART_FALLBACK) as NonNullable<CubeSchema['chart']>;

  switch (chart) {
    case 'scalar':
      return fromScalar(card, payload as ScalarPayload, opts);
    case 'line':
      return fromLine(card, payload as LinePayload, opts);
    case 'bar':
      return fromBar(card, payload as BarPayload, opts);
    case 'table':
      return fromTable(card, payload as TablePayload, opts);
    case 'pie':
      return fromPie(card, payload as PiePayload, opts);
    default:
      return emptyCube(card, opts);
  }
}

/** 表格单元格式化：只做展示缩放，不做任何业务换算。 */
export function formatCell(value: unknown, format?: string): string {
  if (value == null || value === '') return '—';
  const n = toNumber(value);
  if (n == null) return String(value);
  switch (format) {
    case 'wan':
      return (n / 1e4).toLocaleString('zh-CN', { maximumFractionDigits: 1 });
    case 'ratio':
      return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
    case 'pct':
    case 'percent':
      return `${(n * 100).toFixed(1)}%`;
    case 'int':
      return n.toLocaleString('zh-CN');
    default:
      // severity / text / 无 format：原样（severity 由 TableCard 渲染成 chip）
      return String(value);
  }
}

/** 供渲染器只读消费：行主键 → 派生指标。 */
export function derivedOf(cube: CubeSchema, index: number) {
  const key = cube.rowKeys?.[index];
  return key ? cube.derived?.[key] : undefined;
}

export type { Severity };
