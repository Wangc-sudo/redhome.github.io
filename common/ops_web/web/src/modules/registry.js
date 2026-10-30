import {
  hasAnyCapability,
  hasCapability,
  isAdmin,
  me,
  scopedRegions,
} from '../store/me.js';

export const MODULE_REGISTRY_VERSION = 1;

function declaration(value) {
  return {
    version: 1,
    business_line: '*',
    scope: 'global',
    enabled: true,
    feature_flag: null,
    dependencies: [],
    api_router: null,
    migration: null,
    scheduler: null,
    tests: [],
    audit: null,
    ...value,
  };
}

export const modules = [
  declaration({
    module_key: 'flow', workspace: 'business', title: '业务总览', route: 'flow',
    nav_group: '总览', nav_order: 10, component: 'FlowPage', capability: 'org', scope: 'region',
    api_router: 'org', tests: ['flow/graph.test.js'], audit: 'org',
  }),
  declaration({
    module_key: 'data-status', workspace: 'business', title: '数据到位情况', route: 'data-status',
    nav_group: '总览', nav_order: 20, component: 'BusinessFreshnessPage', capability: 'org', scope: 'region',
    api_router: 'business-freshness', tests: ['modules/registry.test.js'],
  }),
  declaration({
    module_key: 'org', workspace: 'business', title: '组织归属', route: 'org',
    nav_group: '组织与目标', nav_order: 30, component: 'OrgPage', capability: 'org', scope: 'region',
    api_router: 'org', audit: 'org_assignment',
  }),
  declaration({
    module_key: 'roster', workspace: 'business', title: '名册与月目标', route: 'roster',
    nav_group: '组织与目标', nav_order: 40, component: 'RosterPage', capability: 'roster', scope: 'region',
    api_router: 'roster', audit: 'roster',
  }),
  declaration({
    module_key: 'calendar', workspace: 'business', title: '播报日历', route: 'calendar',
    nav_group: '业务播报', nav_order: 50, component: 'CalendarPage', capability: 'calendar', scope: 'region',
    api_router: 'calendar', scheduler: 'robot', audit: 'calendar',
  }),
  declaration({
    module_key: 'phones', workspace: 'business', title: '号码台账', route: 'phones',
    nav_group: '业务配置', nav_order: 60, component: 'PhonesPage', capability: 'phone',
    api_router: 'phones', audit: 'phone',
  }),
  declaration({
    module_key: 'data-freshness', workspace: 'admin', title: '数据新鲜度', route: 'data-freshness',
    nav_group: '数据与索引', nav_order: 10, component: 'FreshnessPage', capability: 'pipelines',
    api_router: 'freshness', migration: 'mart-ops-freshness-v1', scheduler: 'dataset-freshness-check',
    tests: ['test_dataset_freshness.py'], audit: 'freshness_check',
  }),
  declaration({
    module_key: 'data-quality', workspace: 'admin', title: '数据质量', route: 'data-quality',
    nav_group: '数据与索引', nav_order: 20, component: 'DataQualityPage', capability: 'pipelines',
    api_router: 'data-quality', scheduler: 'data-quality-check', audit: 'data_quality',
  }),
  declaration({
    module_key: 'pipelines', workspace: 'admin', title: '定时任务', route: 'pipelines',
    nav_group: '任务与运行', nav_order: 30, component: 'PipelinesPage', capability: 'pipelines',
    api_router: 'pipelines', scheduler: 'scheduler', audit: 'pipeline',
  }),
  declaration({
    module_key: 'broadcast', workspace: 'admin', title: '播报编排', route: 'broadcast',
    nav_group: '任务与运行', nav_order: 40, component: 'BroadcastBoardPage', capability: 'pipelines',
    api_router: 'pipelines', audit: 'pipeline',
  }),
  declaration({
    module_key: 'pipelines-history', workspace: 'admin', title: '运行流水', route: 'pipelines/history',
    nav_group: '任务与运行', nav_order: 50, component: 'PipelinesHistoryPage', capability: 'pipelines',
    api_router: 'pipelines',
  }),
  declaration({
    module_key: 'pipelines-audit', workspace: 'admin', title: '任务审计', route: 'pipelines/audit',
    nav_group: '任务与运行', nav_order: 60, component: 'PipelinesAuditPage', capability: 'pipelines',
    api_router: 'pipelines',
  }),
  declaration({
    module_key: 'records', workspace: 'admin', title: '操作记录', route: 'records',
    nav_group: '权限与审计', nav_order: 70, component: 'RecordsPage', capability: 'audit',
    api_router: 'records',
  }),
  declaration({
    module_key: 'recipients', workspace: 'admin', title: '收件人覆盖', route: 'recipients',
    nav_group: '权限与审计', nav_order: 80, component: 'RecipientsPage', capability: 'admin',
    api_router: 'recipients', audit: 'recipient_override',
  }),
  declaration({
    module_key: 'grants', workspace: 'admin', title: '成员授权', route: 'grants',
    nav_group: '权限与审计', nav_order: 90, component: 'GrantsPage', capability: 'admin',
    api_router: 'grants', audit: 'grant',
  }),
];

export function getModule(workspace, moduleKey) {
  return modules.find((item) => (
    item.enabled && item.workspace === workspace && item.module_key === moduleKey
  )) || null;
}

export function canAccessModule(item, businessLine = null) {
  if (!item || !item.enabled) return false;
  if (item.capability === 'admin') return isAdmin();
  if (item.workspace === 'admin') return hasCapability(item.capability);
  if (item.scope === 'region' && businessLine) {
    return hasCapability(item.capability, businessLine);
  }
  return hasAnyCapability(item.capability);
}

export function availableBusinessLines(item) {
  const info = me.value;
  if (!info) return [];
  const registered = info.all_regions || [];
  if (hasCapability(item.capability)) return registered;
  const scoped = scopedRegions(item.capability);
  return registered.filter((key) => scoped.includes(key));
}

export function preferredBusinessLine(item = null) {
  if (item) {
    const lines = availableBusinessLines(item);
    if (lines.length) return lines[0];
  }
  const info = me.value;
  return ((info && (info.regions || [])[0]) || (info && (info.all_regions || [])[0]) || null);
}

export function modulePath(item, businessLine = null) {
  if (item.workspace === 'admin') return `/admin/${item.module_key}`;
  const line = businessLine || preferredBusinessLine(item);
  return line ? `/business/${encodeURIComponent(line)}/${item.module_key}` : null;
}

export function visibleModules(workspace, businessLine = null) {
  return modules
    .filter((item) => item.workspace === workspace && canAccessModule(item, businessLine))
    .sort((a, b) => a.nav_order - b.nav_order);
}

export function firstAccessiblePath() {
  for (const item of visibleModules('business')) {
    const path = modulePath(item);
    if (path) return path;
  }
  const admin = visibleModules('admin')[0];
  return admin ? modulePath(admin) : null;
}
