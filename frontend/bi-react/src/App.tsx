import { Sidebar, type NavItem } from './components/layout/Sidebar';
import { DashboardView } from './components/layout/DashboardView';
import { FALLBACK_NAV, iconOf } from './dashboards/registry';
import { useDashboardList } from './data/useDashboard';
import { useHashRoute } from './router/useHashRoute';

// 壳：Sidebar(导航) + DashboardView(后端 cards[] 驱动)。
// 当前看板来自 **hash 路由** `#/d/{id}`（可刷新、可分享）；后端列表拿不到时退到静态兜底导航。
export default function App() {
  const [routeId] = useHashRoute();
  const { data: list } = useDashboardList();

  const nav: NavItem[] = list?.length
    ? list.map((d) => ({ id: d.id, title: d.title, icon: iconOf(d.id) }))
    : FALLBACK_NAV;

  const activeId = routeId ?? nav[0]?.id;

  return (
    <div className="app-shell">
      <Sidebar items={nav} active={activeId ?? ''} />
      <main className="app-main">
        <DashboardView id={activeId} />
      </main>
    </div>
  );
}
