import { hrefFor } from '../../router/useHashRoute';

// V2 DIGITAL OPS 门户 chrome：侧边栏导航。
// 用真实 <a href="#/d/{id}">：URL 可分享、可刷新还原，不再靠内存状态切页。
export interface NavItem {
  id: string;
  title: string;
  icon?: string;
}

export function Sidebar({ items, active }: { items: NavItem[]; active: string }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark">DO</span>
        <span>
          DIGITAL OPS
          <small>BI 看板</small>
        </span>
      </div>
      <nav>
        {items.map((d) => (
          <a key={d.id} href={hrefFor(d.id)} className={d.id === active ? 'active' : ''}>
            {d.icon && <span aria-hidden>{d.icon}</span>}
            {d.title}
            <span className="nav-dot" />
          </a>
        ))}
      </nav>
    </aside>
  );
}
