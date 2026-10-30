import { createRouter, createWebHistory } from 'vue-router';
import { loadMe, setFlash } from './store/me.js';
import ModuleHost from './modules/ModuleHost.vue';
import {
  canAccessModule,
  firstAccessiblePath,
  getModule,
  modulePath,
} from './modules/registry.js';

const legacyModules = {
  '/overview': ['business', 'flow'],
  '/flow': ['business', 'flow'],
  '/org': ['business', 'org'],
  '/members': ['business', 'org'],
  '/roster': ['business', 'roster'],
  '/calendar': ['business', 'calendar'],
  '/phones': ['business', 'phones'],
  '/data-quality': ['admin', 'data-quality'],
  '/pipelines': ['admin', 'pipelines'],
  '/pipelines/audit': ['admin', 'pipelines-audit'],
  '/pipelines/history': ['admin', 'pipelines-history'],
  '/broadcast': ['admin', 'broadcast'],
  '/records': ['admin', 'records'],
  '/recipients': ['admin', 'recipients'],
  '/grants': ['admin', 'grants'],
  '/audit': ['admin', 'grants'],
};

export const routes = [
  {
    path: '/', name: 'workspace-home', meta: { home: true },
    component: { template: '<div class="state">暂无可访问的工作区，请联系管理员授权</div>' },
  },
  {
    path: '/business/:businessLine/:moduleKey', name: 'business-module',
    component: ModuleHost, meta: { workspace: 'business' },
  },
  {
    path: '/admin/:moduleKey', name: 'admin-module',
    component: ModuleHost, meta: { workspace: 'admin' },
  },
  ...Object.entries(legacyModules).map(([path, [workspace, moduleKey]], index) => ({
    path, name: `legacy-${index}`, component: ModuleHost,
    meta: { legacy: { workspace, moduleKey } },
  })),
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
        window.location.assign('/auth/entry');
        return false;
      }
    }
  }

  if (to.meta.home) {
    const destination = firstAccessiblePath();
    return destination && destination !== '/' ? { path: destination } : true;
  }

  if (to.meta.legacy) {
    const item = getModule(to.meta.legacy.workspace, to.meta.legacy.moduleKey);
    const destination = modulePath(item);
    if (!destination || !canAccessModule(item, item.workspace === 'business' ? destination.split('/')[2] : null)) {
      setFlash('err', '无权限');
      return { path: '/' };
    }
    return { path: destination };
  }

  if (to.meta.workspace) {
    const item = getModule(to.meta.workspace, to.params.moduleKey);
    const businessLine = to.meta.workspace === 'business' ? to.params.businessLine : null;
    if (!item) {
      setFlash('err', '模块不存在或已停用');
      return { path: '/' };
    }
    if (!canAccessModule(item, businessLine)) {
      setFlash('err', '无权限');
      return { path: '/' };
    }
  }
  return true;
});

export default router;
