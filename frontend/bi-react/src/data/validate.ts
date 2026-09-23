/**
 * 运行时数据校验：后端卡片 payload 的形状/值域检查。
 * ---------------------------------------------------------------------------
 * TypeScript 类型只守住编译期；线上 payload 来自 FastAPI 响应，任何后端口径
 * 变更/脏数据都会绕过类型直达渲染器。本模块是**唯一的运行时校验点**
 * （由适配层 cardToCube 调用，渲染器永远不直接消费 payload）：
 *
 *   - 形状：chart 必需数组/对象字段存在且类型正确（缺了 → issue，适配层兜底）；
 *   - 值域：severity 值必须在 payload.severity_domain 内（CubeSchema §2.3：
 *     「前端据此校验而不是硬编码」——域外值上报 issue，渲染层 alertOf 本就不识别）；
 *   - 对齐：line/bar 的序列长度与轴长度不一致 → issue（错位渲染比空态更糟）。
 *
 * 约定：校验只**上报**（issue 列表）+ 适配层**降级**（坏行丢弃/兜底空态），
 * 绝不抛异常打断看板——一张卡的数据问题不拖垮整页（与后端降级同哲学）。
 */

import type { CardPayload } from './types';

export type PayloadIssue = string;

const KNOWN_CHARTS = new Set(['scalar', 'line', 'bar', 'table', 'pie']);

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isCellValue(v: unknown): boolean {
  return v == null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean';
}

function checkArray(v: unknown, path: string, issues: PayloadIssue[]): unknown[] | null {
  if (!Array.isArray(v)) {
    issues.push(`${path}: expected array, got ${v === null ? 'null' : typeof v}`);
    return null;
  }
  return v;
}

function validateTable(p: Record<string, unknown>, issues: PayloadIssue[]): void {
  const columns = checkArray(p.columns, 'table.columns', issues);
  columns?.forEach((c, i) => {
    if (!isObject(c) || typeof c.key !== 'string' || typeof c.title !== 'string') {
      issues.push(`table.columns[${i}]: expected {key: string, title: string}`);
    }
  });

  const rows = checkArray(p.rows, 'table.rows', issues);
  const domain = p.severity_domain;
  if (domain != null && !checkArray(domain, 'table.severity_domain', issues)) {
    // checkArray 已记录 issue；domain 非法时不再做行级值域校验
    return;
  }
  const severityDomain = domain as unknown[] | null;
  rows?.forEach((r, i) => {
    if (!isObject(r)) {
      issues.push(`table.rows[${i}]: expected object row, got ${r === null ? 'null' : typeof r}`);
      return;
    }
    for (const [k, v] of Object.entries(r)) {
      if (!isCellValue(v)) {
        issues.push(`table.rows[${i}].${k}: unsupported cell type ${typeof v}`);
      }
    }
    const sev = r['severity'];
    if (sev != null && severityDomain && !severityDomain.includes(sev)) {
      issues.push(`table.rows[${i}].severity: '${String(sev)}' outside severity_domain`);
    }
  });
}

function validateLine(p: Record<string, unknown>, issues: PayloadIssue[]): void {
  const dates = checkArray(p.dates, 'line.dates', issues);
  const series = checkArray(p.series, 'line.series', issues);
  series?.forEach((s, i) => {
    if (!isObject(s) || typeof s.name !== 'string') {
      issues.push(`line.series[${i}]: expected {name: string, data: array}`);
      return;
    }
    const data = checkArray(s.data, `line.series[${i}].data`, issues);
    if (dates && data && data.length !== dates.length) {
      issues.push(`line.series[${i}].data: length ${data.length} != dates length ${dates.length}`);
    }
  });
}

function validateBar(p: Record<string, unknown>, issues: PayloadIssue[]): void {
  const categories = checkArray(p.categories, 'bar.categories', issues);
  const values = checkArray(p.values, 'bar.values', issues);
  if (categories && values && categories.length !== values.length) {
    issues.push(`bar.values: length ${values.length} != categories length ${categories.length}`);
  }
}

function validatePie(p: Record<string, unknown>, issues: PayloadIssue[]): void {
  const items = checkArray(p.items, 'pie.items', issues);
  items?.forEach((it, i) => {
    if (!isObject(it) || typeof it.name !== 'string') {
      issues.push(`pie.items[${i}]: expected {name: string, value: number|null}`);
    }
  });
}

function validateScalar(p: Record<string, unknown>, issues: PayloadIssue[]): void {
  const v = p.value;
  if (v != null && typeof v !== 'number') {
    issues.push(`scalar.value: expected number|null, got ${typeof v}`);
  }
}

/**
 * 校验后端卡片 payload，返回 issue 列表（空数组 = 通过）。
 * 入参按 unknown 处理——这正是运行时校验存在的意义。
 */
export function validateCardPayload(payload: unknown): PayloadIssue[] {
  const issues: PayloadIssue[] = [];
  if (!isObject(payload)) {
    return [`payload: expected object, got ${payload === null ? 'null' : typeof payload}`];
  }
  const chart = payload.chart;
  if (typeof chart !== 'string' || !KNOWN_CHARTS.has(chart)) {
    issues.push(`payload.chart: unknown chart '${String(chart)}'`);
    return issues; // chart 不明时分派无意义，其余字段不再查
  }
  switch (chart) {
    case 'table':
      validateTable(payload, issues);
      break;
    case 'line':
      validateLine(payload, issues);
      break;
    case 'bar':
      validateBar(payload, issues);
      break;
    case 'pie':
      validatePie(payload, issues);
      break;
    case 'scalar':
      validateScalar(payload, issues);
      break;
  }
  return issues;
}

/**
 * 表格行降级过滤：丢弃非对象行（对应 validateTable 的 rows[i] issue）。
 * 合法行原样保留（单元格脏值由 formatCell/alertOf 各自的防御逻辑兜底）。
 */
export function sanitizeTableRows(rows: unknown): Record<string, unknown>[] {
  if (!Array.isArray(rows)) return [];
  return rows.filter(isObject);
}

export type { CardPayload };
