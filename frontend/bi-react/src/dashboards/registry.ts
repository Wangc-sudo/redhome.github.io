/**
 * 看板注册表（**静态兜底**）
 * ---------------------------------------------------------------------------
 * 变更说明（契约适配）：旧设计由本文件静态声明每块看板的 widgets（x/y/w/h + config），
 * 但真实后端是**卡片驱动**的 —— `GET /api/v1/dashboards/{id}` 直接给出 cards[]（含 span/chart/params）。
 * 再维护一份静态 widget 定义就会出现「两处定义打架」，因此：
 *   - 布局 / 卡片 / 筛选 一律以后端 cards[] 为准（见 components/layout/DashboardView.tsx）
 *   - 本文件只保留 `/api/v1/dashboards` 不可用时的**导航兜底** + 图标映射
 * 后续若确需前端自定义看板（后端没有的模板），再在此扩展，并同步 PROGRESS.md。
 */

export interface NavItem {
  id: string;
  title: string;
  icon?: string;
}

const ICON: Record<string, string> = {
  'l1-cockpit': '🚨',
  'l2-region': '🗺️',
  'l2-channel': '📡',
  'l2-product': '📦',
  'l2-people': '👤',
};

export const DEFAULT_DASHBOARD = 'l1-cockpit';

export const FALLBACK_NAV: NavItem[] = [
  { id: 'l1-cockpit', title: '首页驾驶舱', icon: ICON['l1-cockpit'] },
  { id: 'l2-region', title: '区域下钻', icon: ICON['l2-region'] },
  { id: 'l2-channel', title: '渠道明细', icon: ICON['l2-channel'] },
  { id: 'l2-product', title: '商品动销', icon: ICON['l2-product'] },
  { id: 'l2-people', title: '人员榜', icon: ICON['l2-people'] },
];

export function iconOf(id: string): string | undefined {
  return ICON[id];
}
