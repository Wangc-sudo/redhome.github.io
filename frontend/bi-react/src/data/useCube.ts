/**
 * 取数层：看板级共享缓存 + 后端 refresh_seconds 驱动轮询 + AbortController
 * ---------------------------------------------------------------------------
 * 修掉的开工缺陷：
 *  1) 旧实现「每卡一个 30s 定时」→ N 张卡 = N 倍请求。现在同一 key 共享一份
 *     缓存与一条轮询链（并发订阅只会触发一次 in-flight 请求）。
 *  2) 轮询周期硬编码 30s → 现在取后端 dashboard.refresh_seconds（实测 300s）。
 *  3) 卸载/切看板只置 alive=false，请求仍在飞 → 现在用 AbortController 真取消，
 *     最后一个订阅者离开时 abort + 清定时器 + 释放缓存。
 *
 * 组件只 const { data, loading, error } = useCardCube(...)，禁止直接 fetch。
 */

import { useCallback, useRef, useSyncExternalStore } from 'react';
import type { CubeSchema } from '../types/cube';
import { cardToCube } from './adapter/cardToCube';
import { fetchCard, pickParams } from './biWebClient';
import type { CardDef } from './types';

export interface ResourceState<T> {
  data?: T;
  loading: boolean;
  error?: Error;
}

const LOADING: ResourceState<never> = { loading: true };

type Loader<T> = (signal: AbortSignal) => Promise<T>;

interface Entry<T> {
  key: string;
  state: ResourceState<T>;
  subs: Set<() => void>;
  loader: Loader<T>;
  refreshMs: number;
  timer?: ReturnType<typeof setTimeout>;
  ctrl?: AbortController;
}

const store = new Map<string, Entry<unknown>>();

function setState<T>(e: Entry<T>, next: ResourceState<T>): void {
  e.state = next;
  e.subs.forEach((cb) => cb());
}

function schedule<T>(e: Entry<T>): void {
  clearTimeout(e.timer);
  e.timer = undefined;
  if (e.refreshMs > 0 && e.subs.size > 0) {
    e.timer = setTimeout(() => {
      if (e.subs.size > 0) void run(e);
    }, e.refreshMs);
  }
}

async function run<T>(e: Entry<T>): Promise<void> {
  e.ctrl?.abort();
  const ctrl = new AbortController();
  e.ctrl = ctrl;

  setState(e, { ...e.state, loading: true });

  try {
    const data = await e.loader(ctrl.signal);
    if (e.ctrl !== ctrl) return; // 已被新一次请求取代（切看板/改参数）
    setState(e, { data, loading: false });
  } catch (err) {
    if (e.ctrl !== ctrl) return;
    if ((err as Error)?.name === 'AbortError') return;
    setState(e, { data: e.state.data, loading: false, error: err as Error });
  } finally {
    if (e.ctrl === ctrl) schedule(e);
  }
}

function unsubscribe<T>(e: Entry<T>, cb: () => void): void {
  e.subs.delete(cb);
  if (e.subs.size > 0) return;
  // 最后一个订阅者离开：取消在飞请求 + 停轮询 + 释放缓存
  clearTimeout(e.timer);
  e.timer = undefined;
  e.ctrl?.abort();
  e.ctrl = undefined;
  store.delete(e.key);
}

function subscribe<T>(key: string, loader: Loader<T>, refreshMs: number, cb: () => void): () => void {
  let e = store.get(key) as Entry<T> | undefined;

  if (!e) {
    e = { key, state: { loading: true }, subs: new Set(), loader, refreshMs };
    store.set(key, e as Entry<unknown>);
    e.subs.add(cb);
    void run(e);
    return () => unsubscribe(e as Entry<T>, cb);
  }

  e.subs.add(cb);
  e.loader = loader; // 闭包可能持有新的 props（params / card 定义）
  if (e.refreshMs !== refreshMs) {
    e.refreshMs = refreshMs;
    if (!e.state.loading) schedule(e);
  }
  return () => unsubscribe(e as Entry<T>, cb);
}

/** 通用：带缓存 + 共享轮询 + 自动取消的取数 hook。key=null 时不取数。 */
export function usePolled<T>(key: string | null, loader: Loader<T>, refreshMs = 0): ResourceState<T> {
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const subscribeFn = useCallback(
    (cb: () => void) => {
      if (!key) return () => undefined;
      return subscribe<T>(key, (signal) => loaderRef.current(signal), refreshMs, cb);
    },
    [key, refreshMs],
  );

  const getSnapshot = useCallback(() => {
    if (!key) return LOADING as ResourceState<T>;
    return (store.get(key) as Entry<T> | undefined)?.state ?? (LOADING as ResourceState<T>);
  }, [key]);

  return useSyncExternalStore(subscribeFn, getSnapshot, getSnapshot);
}

/** 手动重试（错误态「重试」按钮）。 */
export function refreshResource(key: string): void {
  const e = store.get(key);
  if (e) void run(e as Entry<unknown>);
}

/* --------------------------------------------------------------- 卡片取数 */

export type CubeState = ResourceState<CubeSchema>;

export function paramsKey(params: Record<string, string>): string {
  return Object.entries(params)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${v}`)
    .join('&');
}

/** 卡片缓存 key：看板 + 卡 + 参数（参数变了才重取，否则命中缓存）。 */
export function cardKey(dashboardId: string, card: string, params: Record<string, string>): string {
  const qs = paramsKey(params);
  return qs ? `card:${dashboardId}/${card}?${qs}` : `card:${dashboardId}/${card}`;
}

/**
 * 取一张卡，并按 chart 归一成 CubeSchema。
 * refreshMs 由后端 dashboard.refresh_seconds 提供（0 = 不轮询）。
 */
export function useCardCube(
  dashboardId: string | undefined,
  card: CardDef | undefined,
  params: Record<string, string>,
  refreshMs = 0,
): CubeState {
  const key = dashboardId && card ? cardKey(dashboardId, card.card, pickParams(card, params)) : null;

  const loader = useCallback<Loader<CubeSchema>>(
    async (signal) => {
      if (!dashboardId || !card) throw new Error('缺少看板或卡片定义');
      const payload = await fetchCard(dashboardId, card.card, pickParams(card, params), signal);
      return cardToCube(card, payload, { dashboardId });
    },
    [dashboardId, card, paramsKey(params)],
  );

  return usePolled<CubeSchema>(key, loader, refreshMs);
}

/**
 * 注意：不要在循环里调用 useCardCube（卡片数量随看板变化 → hook 数量会变，违反 Rules of
 * Hooks）。正确姿势是每张卡渲染成独立的 `<CardWidget>` 组件，各自订阅共享缓存 ——
 * 这正是 renderWidget.tsx 旧实现（map 里调 useCube）踩的坑。
 */
