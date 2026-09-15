/**
 * 纯展示格式化（单位缩放 / 百分比 / 涨跌方向 class）。
 * ⚠️ 这里**只格式化**，不做任何缺口/告警/环比的业务计算（那是 derive.ts 的唯一职责）。
 * 单位缩放口径见 bi-ui/CubeSchema.md §6：后端给原值 + unit，前端只缩放显示。
 */

export interface Scaled {
  text: string;
  unit: string;
}

/** 金额缩放：≥1亿→「1.22亿」，≥1万→「453.4万」，否则原值。 */
export function scaleMoney(n: number, unit?: string | null): Scaled {
  if (!Number.isFinite(n)) return { text: '—', unit: unit ?? '' };
  if (unit === '万') return { text: n.toLocaleString('zh-CN', { maximumFractionDigits: 1 }), unit: '万' };
  if (unit === '亿') return { text: n.toLocaleString('zh-CN', { maximumFractionDigits: 2 }), unit: '亿' };

  const abs = Math.abs(n);
  if (abs >= 1e8) return { text: (n / 1e8).toLocaleString('zh-CN', { maximumFractionDigits: 2 }), unit: '亿' };
  if (abs >= 1e4) return { text: (n / 1e4).toLocaleString('zh-CN', { maximumFractionDigits: 1 }), unit: '万' };
  return { text: n.toLocaleString('zh-CN', { maximumFractionDigits: 0 }), unit: unit ?? '' };
}

export function formatPct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

/** 涨跌：涨红跌绿（国内惯例，bi-ui/components.css .delta.up/.down） */
export function deltaClass(d?: number | null): string {
  if (d == null || !Number.isFinite(d)) return '';
  if (d > 0) return 'delta up';
  if (d < 0) return 'delta down';
  return 'delta flat';
}

export function formatDelta(d?: number | null): string {
  if (d == null || !Number.isFinite(d)) return '—';
  const sign = d > 0 ? '+' : '';
  return `${sign}${d.toFixed(1)}%`;
}
