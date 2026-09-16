// 类型化 CubeSchema —— 对接 bi-ui/CubeSchema.md 的数据口径契约。
// 关键约定：缺口 / 进度差 / 告警级别 / 环比 一律由后端算好放进 derived，
// 前端（任何渲染器）只读取、不计算。

export type FieldType = 'dimension' | 'measure';
export type DataType = 'string' | 'number' | 'percent' | 'currency' | 'date';
export type Severity = 'p0' | 'p1' | 'p2' | 'ok';

export interface FieldDef {
  key: string;
  label: string;
  type: FieldType;
  dataType: DataType;
  unit?: string;
  /** 后端表格列的展示格式（wan/ratio/int/pct/text），前端只格式化不换算语义 */
  format?: string;
  /** 派生列（缺口/完成率/告警…）由后端产出，前端只读 */
  derived?: boolean;
}

/** 单元格取值。boolean 是后端 2026-09-15 新增的诊断字段 `has_fact` 引入的（非展示值）。 */
export type CubeValue = string | number | boolean | null;

export interface CubeRow {
  [key: string]: CubeValue;
}

/** 行级派生指标：全部由后端算，前端只渲染。 */
export interface DerivedMetric {
  target?: number; // 目标
  done?: number; // 已完成
  shortfall?: number; // 缺口 = 应完成 - 已完成（绝对，不锚时间进度）
  requiredDaily?: number; // 所需日销 = 缺口 / 剩余工作日
  progressRate?: number; // 0..1
  delta?: number; // 环比：正=红(涨) 负=绿(跌)，由 --up/--down 决定
  alert?: AlertInfo; // 四级告警（绝对缺口/所需日销锚定，月内稳定）
  confidence?: 'high' | 'low'; // 低置信：如预测 173%，前端显式标注
  missing?: boolean; // 数据缺失：显示 — 且不参与排序
}

export interface AlertInfo {
  severity: Severity;
  label: string; // "P0"
  reason: string; // 文字说明，色盲友好（不只靠颜色）
}

export interface HierarchyDef {
  field: string; // 层系字段
  levels: string[]; // 下钻顺序，如 ['region','channel','product']
}

export interface CubeSchema {
  id: string;
  title: string;
  /** 源卡类型（后端 chart 字段），渲染器据此选图形，不反推业务含义 */
  chart?: 'scalar' | 'line' | 'bar' | 'table' | 'pie';
  /** 金额/数值单位（元/万/…），由后端 payload 透传 */
  unit?: string | null;
  dimensions: FieldDef[];
  measures: FieldDef[];
  hierarchies?: HierarchyDef[];
  rows: CubeRow[];
  /**
   * 与 rows 一一对应的行主键，用于在 `derived` 里取该行的派生指标。
   * 表格/饼图/柱图 = 维度取值；折线图 = 日期；标量卡 = 卡片 id。
   */
  rowKeys?: string[];
  /** 表格列定义（优先于 dimensions+measures，保证列序与后端一致） */
  columns?: FieldDef[];
  /** key = 行主键；行级派生指标一律由后端算好（前端无派生实现，缺了渲染「—」）。 */
  derived?: Record<string, DerivedMetric>;
  asOf?: string; // 数据时间
  updatedAt?: string;
}

export class CubeError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'CubeError';
  }
}
