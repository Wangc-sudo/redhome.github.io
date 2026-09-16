/**
 * 交互中心：层系下钻 + 图表联动事件总线。
 *
 * 状态：
 *   path：当前看板 id
 *   drillLevel：下钻层级（0=默认，1+=更深层）
 *   filters：筛选参数（后端透传，不过滤派生指标）
 *   bus：极简事件总线（emit/on，供图表联动）
 *
 * 下钻：hierarchies.levels 顺序推进，setPath 触发 App 重新 useCube 取下一层 cube
 * 联动：emit('filter',{dim,value}) → 其他 widget on 监听后加 filter 重取
 */
import { create } from 'zustand';
// 下钻：hierarchies.levels 顺序推进，setPath 触发 App 重新 useCube 取下一层 cube。
// 联动：emit('filter',{dim,value}) → 其他 widget 的 on 监听后加 filter 重取。
type Listener = (payload: unknown) => void;

interface DrillState {
  path: string;
  drillLevel: number;
  filters: Record<string, string>;
  setPath: (p: string) => void;
  drillDown: (field: string) => void;
  drillUp: () => void;
  setFilter: (k: string, v: string) => void;
  // 极简事件总线
  bus: Map<string, Set<Listener>>;
  emit: (event: string, payload: unknown) => void;
  on: (event: string, cb: Listener) => () => void;
}

export const useDrill = create<DrillState>((set, get) => ({
  path: 'l1-cockpit',
  drillLevel: 0,
  filters: {},
  setPath: (p) => set({ path: p, drillLevel: 0, filters: {} }),
  drillDown: (field) =>
    set((s) => ({ drillLevel: s.drillLevel + 1, filters: { ...s.filters, level: field } })),
  drillUp: () => set((s) => ({ drillLevel: Math.max(0, s.drillLevel - 1) })),
  setFilter: (k, v) => set((s) => ({ filters: { ...s.filters, [k]: v } })),
  bus: new Map(),
  emit: (event, payload) => {
    get().bus.get(event)?.forEach((cb) => cb(payload));
  },
  on: (event, cb) => {
    const bus = get().bus;
    if (!bus.has(event)) bus.set(event, new Set());
    bus.get(event)!.add(cb);
    return () => bus.get(event)!.delete(cb);
  },
}));
