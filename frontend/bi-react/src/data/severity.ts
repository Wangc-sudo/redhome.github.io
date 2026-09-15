/**
 * 四级告警的**展示映射**（色/文案），与 CubeSchema.md §2.3 的值域一致。
 * ---------------------------------------------------------------------------
 * 这里只有 code → {label, reason} 的查表，**没有任何算式**（缺口/告警级别的
 * 计算只在后端 derived.py；过渡期前端副本只存在于 data/derive.ts）。
 *
 * 为什么独立成文件：后端已经自己产出 severity（kpi_shortfall / anomaly_top），
 * 前端只需把它装饰成 alert-chip；这份映射在 derive.ts 被删之后仍然要保留，
 * 所以不能跟着 derive.ts 一起走。
 */

import type { AlertInfo, Severity } from '../types/cube';

/** 后端 derived.SEVERITY_DOMAIN 的前端镜像（顺序即严重度降序）。 */
export const SEVERITY_DOMAIN: readonly Severity[] = ['p0', 'p1', 'p2', 'ok'] as const;

const LABEL: Record<Severity, string> = { p0: 'P0', p1: 'P1', p2: 'P2', ok: '正常' };

/** 文字说明：色盲友好，chip 不能只靠颜色（VISUALIZATION §8 / CubeSchema §7） */
const REASON: Record<Severity, string> = {
  p0: '零动销，今日跟进',
  p1: '缺口超半月产能，本周内处理',
  p2: '有缺口，持续观察',
  ok: '已达标',
};

export function isSeverity(v: unknown): v is Severity {
  return typeof v === 'string' && (SEVERITY_DOMAIN as readonly string[]).includes(v);
}

/** severity code → AlertInfo；不在值域内返回 undefined（不臆造告警）。 */
export function alertOf(severity: unknown): AlertInfo | undefined {
  if (!isSeverity(severity)) return undefined;
  return { severity, label: LABEL[severity], reason: REASON[severity] };
}
