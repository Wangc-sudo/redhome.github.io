import { defineAsyncComponent } from 'vue';

const loaders = {
  FlowPage: () => import('../pages/FlowPage.vue'),
  BusinessFreshnessPage: () => import('../pages/BusinessFreshnessPage.vue'),
  OrgPage: () => import('../pages/OrgPage.vue'),
  RosterPage: () => import('../pages/RosterPage.vue'),
  CalendarPage: () => import('../pages/CalendarPage.vue'),
  PhonesPage: () => import('../pages/PhonesPage.vue'),
  FreshnessPage: () => import('../pages/FreshnessPage.vue'),
  DataQualityPage: () => import('../pages/DataQualityPage.vue'),
  PipelinesPage: () => import('../pages/PipelinesPage.vue'),
  BroadcastBoardPage: () => import('../pages/BroadcastBoardPage.vue'),
  PipelinesHistoryPage: () => import('../pages/PipelinesHistoryPage.vue'),
  PipelinesAuditPage: () => import('../pages/PipelinesAuditPage.vue'),
  RecordsPage: () => import('../pages/RecordsPage.vue'),
  RecipientsPage: () => import('../pages/RecipientsPage.vue'),
  GrantsPage: () => import('../pages/GrantsPage.vue'),
};

export function resolveComponent(componentKey) {
  const loader = loaders[componentKey];
  if (!loader) throw new Error(`未注册的前端组件：${componentKey}`);
  return defineAsyncComponent({
    loader,
    loadingComponent: { template: '<div class="state">模块加载中…</div>' },
    errorComponent: { template: '<div class="state error">模块加载失败，请刷新重试</div>' },
  });
}
