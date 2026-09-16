/**
 * 四级告警的**展示映射**（色/文案），与 CubeSchema.md §2.3 的值域一致。
 * ---------------------------------------------------------------------------
 * 这里只有 code → {label, reason} 的查表，**没有任何算式**（缺口/告警级别的
 * 计算只在后端 derived.py；前端过渡副本 derive.ts 已按路线图 P3 整文件删除）。
 *
 * 为什么独立成文件：后端自己产出 severity（kpi_shortfall / anomaly_top），
 * 前端只需把它装饰成 alert-chip；这份映射是 chip 展示的唯一依赖，长期保留。
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

/**
 * 占位卡（卡级 has_fact=false）的告警 chip：应接入未接入。
 * ---------------------------------------------------------------------------
 * 语义：该卡应接入数据源但尚未接入（0 占位），按 p0 渲染（指南裁决：挂零 = p0）。
 * label/severity 复用上表查表，reason 是占位卡专属文案 —— 文案集中在
 * 本文件这一处，组件不自造第二份文案表。
 */
export function noFactCardAlert(): AlertInfo {
  return { severity: 'p0', label: LABEL.p0, reason: '应接入未接入' };
}
