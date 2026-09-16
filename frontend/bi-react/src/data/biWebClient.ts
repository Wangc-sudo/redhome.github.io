/**
 * bi_web 真实契约客户端（唯一 fetch 出口，组件禁止直接 fetch）
 * ---------------------------------------------------------------------------
 * 后端实测契约（2026-09-15，http://127.0.0.1:18080）：
 *   GET /api/v1/dashboards                 → { dashboards: [{id,title}] }
 *   GET /api/v1/dashboards/{id}            → { id,title,refresh_seconds,filters[],cards[] }
 *   GET /api/v1/options/{source}           → { source, options: string[] }
 *   GET /api/v1/d/{id}/cards/{card}?params → 卡片 payload（chart ∈ scalar|line|bar|table|pie）
 *
 * ⚠️ 与 ARCHITECTURE.md 的**假设契约不同**：原设计假设 GET /api/v1/d/{path} 直接返回
 * CubeSchema，后端并不存在该端点（实测 404）。因此本文件只做「取数 + 统一错误语义」，
 * 由 `adapter/cardToCube.ts` 把卡片 payload 归一成 CubeSchema。差异清单见 PROGRESS.md。
 */

import { mockFixtures, type MockFixtures } from './mock/fixtures';
import { BiWebError, kindOfStatus } from './errors';
import type { CardDef, DashboardDefinition, DashboardSummary, CardPayload } from './types';

export { BiWebError } from './errors';
export type { BiErrorKind } from './errors';
export type { CardDef, ChartKind, DashboardDefinition, DashboardSummary, CardPayload } from './types';

export const API_BASE = '/api/v1';

/** VITE_MOCK=1 → 全量走本地 fixture，后端不启也能开发/演示。 */
export const MOCK: boolean = import.meta.env.VITE_MOCK === '1';

const TOKEN: string = (import.meta.env.VITE_BI_WEB_TOKEN ?? '').trim();

/* -------------------------------------------------------------------- 请求核 */

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      signal,
      headers: {
        Accept: 'application/json',
        ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
      },
    });
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e;
    throw new BiWebError('network', 0, `无法连接后端：${String((e as Error)?.message ?? e)}`);
  }

  if (!res.ok) throw new BiWebError(kindOfStatus(res.status), res.status, `${res.status} ${res.statusText}`);

  try {
    return (await res.json()) as T;
  } catch {
    throw new BiWebError('network', res.status, '响应不是合法 JSON');
  }
}

function encodeQuery(params: Record<string, string>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== '') usp.set(k, v);
  }
  const qs = usp.toString();
  return qs ? `?${qs}` : '';
}

/* ------------------------------------------------------------------ 四个端点 */

export const OPTION_SOURCES = ['regions', 'channels', 'months', 'brands', 'sku_channels'] as const;
export type OptionSource = (typeof OPTION_SOURCES)[number];

/** 只把该卡声明过的参数键带上，避免后端 400 bad_request。 */
export function pickParams(card: CardDef, params: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const key of card.params ?? []) {
    const v = params[key];
    if (v != null && v !== '') out[key] = v;
  }
  return out;
}

export async function fetchDashboards(signal?: AbortSignal): Promise<DashboardSummary[]> {
  if (MOCK) return mockFixtures.listDashboards();
  const json = await request<{ dashboards: DashboardSummary[] }>('/dashboards', signal);
  return json.dashboards ?? [];
}

export async function fetchDashboardDef(id: string, signal?: AbortSignal): Promise<DashboardDefinition> {
  if (MOCK) return mockFixtures.getDashboardDef(id);
  return await request<DashboardDefinition>(`/dashboards/${encodeURIComponent(id)}`, signal);
}

export async function fetchOptions(source: string, signal?: AbortSignal): Promise<string[]> {
  if (MOCK) return mockFixtures.getOptions(source);
  const json = await request<{ source: string; options: string[] }>(
    `/options/${encodeURIComponent(source)}`,
    signal,
  );
  return json.options ?? [];
}

export async function fetchCard(
  dashboardId: string,
  cardId: string,
  params: Record<string, string> = {},
  signal?: AbortSignal,
): Promise<CardPayload> {
  if (MOCK) return mockFixtures.getCardPayload(dashboardId, cardId);
  const qs = encodeQuery(params);
  return await request<CardPayload>(
    `/d/${encodeURIComponent(dashboardId)}/cards/${encodeURIComponent(cardId)}${qs}`,
    signal,
  );
}

/** 供测试/调试：拿到 mock 后端实例。 */
export function mockBackend(): MockFixtures {
  return mockFixtures;
}
