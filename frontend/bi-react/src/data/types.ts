/**
 * bi_web 后端契约类型（与 common/bi_web/app.py 实测响应一一对应）
 * ---------------------------------------------------------------------------
 * 这里只描述「后端现在长什么样」；CubeSchema（前端渲染契约）在 types/cube.ts，
 * 两者的转换在 adapter/cardToCube.ts。不允许在渲染器里直接消费本文件的类型。
 */

/* ------------------------------------------------------------ 看板 / 卡片定义 */

export interface DashboardSummary {
  id: string;
  title: string;
}

export type ChartKind = 'scalar' | 'line' | 'bar' | 'table' | 'pie';

export interface CardDef {
  /** 卡片 id，取数路径 /d/{dashboard}/cards/{card} */
  card: string;
  title: string;
  chart: ChartKind;
  /** 12 栅格占位宽度（后端语义），前端据此算 react-grid-layout 的 w */
  span: number;
  on_click: string | null;
  /** 该卡接受的下拉参数名（只有这些键可以进 query，否则后端 400） */
  params: string[];
}

export interface FilterDef {
  param: string;
  source: string;
  label: string;
}

export interface DashboardDefinition {
  id: string;
  title: string;
  /** 轮询周期（秒）——前端取数周期以此为准，不再硬编码 30s */
  refresh_seconds: number;
  filters: FilterDef[];
  cards: CardDef[];
}

/* ---------------------------------------------------------------- 卡片 payload */

export interface TrendPoint {
  date: string;
  value: number | null;
}

export interface ScalarPayload {
  chart: 'scalar';
  value: number | null;
  unit?: string | null;
  /** 有目标的 KPI（如年度达成）：target + rate */
  target?: number | null;
  rate?: number | null;
  /** 日环比 KPI：date / prev / delta_pct / trend7 */
  date?: string | null;
  prev?: number | null;
  delta_pct?: number | null;
  trend7?: TrendPoint[] | null;
}

export interface LineSeries {
  name: string;
  data: (number | null)[];
}

export interface LinePayload {
  chart: 'line';
  dates: string[];
  series: LineSeries[];
  unit?: string | null;
}

export interface BarPayload {
  chart: 'bar';
  categories: string[];
  values: (number | null)[];
  unit?: string | null;
}

export type ColumnFormat = 'wan' | 'ratio' | 'int' | 'pct' | 'percent' | 'severity' | 'text' | null;

export interface TableColumnDef {
  key: string;
  title: string;
  format?: ColumnFormat;
}

export interface TablePayload {
  chart: 'table';
  columns: TableColumnDef[];
  rows: Record<string, unknown>[];
  /** 金额/数值单位（派生卡如 kpi_shortfall 也带） */
  unit?: string | null;
  /** 数据截止时间（后端 as_of，派生卡带） */
  as_of?: string | null;
  /** 统计月份 / 汇总粒度 / TOP N —— 派生卡的附加元信息，前端只透传展示 */
  month?: string | null;
  grain?: string | null;
  limit?: number | null;
  /** 后端 severity 值域（CubeSchema §2.3），前端据此校验而不是硬编码 */
  severity_domain?: string[];
  /** 卡级挂零：占位卡（应接入未接入）为 false，行级 has_fact 不动；缺省/true = 正常卡 */
  has_fact?: boolean;
}

export interface PieItem {
  name: string;
  value: number | null;
}

export interface PiePayload {
  chart: 'pie';
  items: PieItem[];
  unit?: string | null;
}

export type CardPayload = ScalarPayload | LinePayload | BarPayload | TablePayload | PiePayload;
