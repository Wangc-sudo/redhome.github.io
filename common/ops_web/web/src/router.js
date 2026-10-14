/**
 * 全 SPA 路由表 + 守卫（C 阶段）。
 *
 * 页面静态 import（无懒加载）。守卫：首次进入 await loadMe()；
 * 401/403 → 跳 /auth/entry；meta.capability 不通过 → 重定向 / 并 flash。
 */
import { createRouter, createWebHistory } from 'vue-router';
import { loadMe, hasCapability, isAdmin, setFlash } from './store/me.js';
import OverviewPage from './pages/OverviewPage.vue';
import OrgPage from './pages/OrgPage.vue';
import RosterPage from './pages/RosterPage.vue';
import RecipientsPage from './pages/RecipientsPage.vue';
import CalendarPage from './pages/CalendarPage.vue';
import PipelinesPage from './pages/PipelinesPage.vue';
import PipelinesAuditPage from './pages/PipelinesAuditPage.vue';
import PipelinesHistoryPage from './pages/PipelinesHistoryPage.vue';
import RecordsPage from './pages/RecordsPage.vue';
import GrantsPage from './pages/GrantsPage.vue';

const routes = [
  { path: '/', name: 'overview', component: OverviewPage },
  {
    path: '/org',
    alias: '/members',
    name: 'org',
    component: OrgPage,
    meta: { capability: 'org' },
  },
  { path: '/roster', name: 'roster', component: RosterPage, meta: { capability: 'roster' } },
  { path: '/recipients', name: 'recipients', component: RecipientsPage, meta: { capability: 'admin' } },
  { path: '/calendar', name: 'calendar', component: CalendarPage, meta: { capability: 'calendar' } },
  { path: '/pipelines', name: 'pipelines', component: PipelinesPage, meta: { capability: 'pipelines' } },
  {
    path: '/pipelines/audit',
    name: 'pipelines-audit',
    component: PipelinesAuditPage,
    meta: { capability: 'pipelines' },
  },
  {
    path: '/pipelines/history',
    name: 'pipelines-history',
    component: PipelinesHistoryPage,
    meta: { capability: 'pipelines' },
  },
  { path: '/records', name: 'records', component: RecordsPage, meta: { capability: 'audit' } },
  {
    path: '/grants',
    alias: '/audit',
    name: 'grants',
    component: GrantsPage,
    meta: { capability: 'admin' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

let meLoaded = false;

router.beforeEach(async (to) => {
  if (!meLoaded) {
    try {
      await loadMe();
      meLoaded = true;
    } catch (err) {
      if (err && (err.status === 401 || err.status === 403)) {
        // 未登录/未准入：交由后端登录入口处理。
        window.location.assign('/auth/entry');
        return false;
      }
      // 其它错误（网络/503）：不拦截，交由页面自身处理。
    }
  }

  const cap = to.meta && to.meta.capability;
  if (cap) {
    const ok = cap === 'admin' ? isAdmin() : hasCapability(cap);
    if (!ok) {
      setFlash('err', '无权限');
      return { path: '/' };
    }
  }
  return true;
});

export default router;
