/**
 * 闭环画布·建图纯函数（2026-10-16 F1 从 FlowPage.vue 抽出，行为零变化）。
 * 零依赖（不 import vue）：输入 load() 的原始数据 {overview, roster, groups}，
 * 输出节点/边模型 {poolMembers, regionNodes, taskNodes, edges}——可单测。
 * 节点上的 x/y 是占位默认值，由 layout.js 的 layoutNodes 统一覆写。
 */
import { REGION_X, TASK_X, TOP_Y } from './layout.js';

//: 聚合区域 → 组成它的区域键。聚合节点指标=成员桶求和，不做名册门禁
//: （它不填报，只汇总）；形态=圆形（与矩形业务节点视觉分层）。
export const AGGREGATE_REGIONS = {
  offline_all: { label: '线下整体', parts: ['hangzhou', 'shaoxing', 'other'] },
};

//: 补集桶虚拟节点（「其他」：名册有余云涛/谢坚钰/李树军但组织树无成员，
//: overview.regions 不含它——从名册/目标数据派生）。
export const COMPLEMENT_REGION = 'other';

//: 平台线（region=null 的服务组）的虚拟区域键。
export const PLATFORM_KEY = '__platform__';

//: 未归类池最多展示人数（超出截断，防节点过长）。
export const POOL_LIMIT = 50;

/** 管道家族键：剥掉区域后缀（robot-check-hangzhou → robot-check）。 */
export function familyOf(serviceId, regionKey) {
  const suffix = `-${regionKey}`;
  if (regionKey !== PLATFORM_KEY && serviceId.endsWith(suffix)) {
    return serviceId.slice(0, -suffix.length);
  }
  return serviceId;
}

/** 家族展示名（先映射表，缺省用首成员的类别标签）。 */
const FAMILY_LABEL = {
  'robot': '填报提醒',
  'robot-check': '催办未填 + DING',
  'pages': '榜单页生成',
  'leaderboard': '榜单群播报',
  'leaderboard-html': '榜单页生成',
  'channel-missing-check': '渠道到齐检查',
};
export function familyLabel(family, firstService) {
  return FAMILY_LABEL[family]
    || `${(firstService && firstService.category_label) || '任务'}·${family}`;
}

export function regionBadge(rosterCount, targetCount, taskCount) {
  if (rosterCount === 0) return { text: '缺名册', cls: 'warn' };
  if (targetCount === 0) return { text: '缺目标', cls: 'warn' };
  if (!taskCount) return { text: '待下游', cls: 'warn' };
  return { text: '已闭环', cls: 'ok' };
}

export function fmt(value) {
  const num = Number(value) || 0;
  return Number.isInteger(num) ? String(num) : num.toFixed(1);
}

export function taskBroken(service) {
  if (!service.enabled) return true;
  const latest = service.latest;
  return !!(latest && latest.status === 'failed');
}

/**
 * 一个区域的服务按家族折叠为模块节点 + 区域→模块边（普通/聚合两路共用，
 * 2026-10-16 消除双循环重复）。badge 传入时 warn 徽标让边标红（仅普通区域）。
 */
function collectFamilyModules(key, services, { badge = null } = {}) {
  const families = {};
  for (const service of services) {
    const family = familyOf(service.service_id, key);
    if (!families[family]) families[family] = [];
    families[family].push(service);
  }
  const tasks = [];
  const edges = [];
  for (const [family, members] of Object.entries(families)) {
    const moduleId = `module:${key}:${family}`;
    const brokenCount = members.filter(taskBroken).length;
    tasks.push({
      id: moduleId,
      regionKey: key,
      family,
      label: familyLabel(family, members[0]),
      services: members,
      brokenCount,
      x: TASK_X,
      y: TOP_Y,
    });
    edges.push({
      id: `e:${key}:${family}`,
      from: `region:${key}`,
      to: moduleId,
      broken: (badge && badge.cls === 'warn') || brokenCount > 0,
    });
  }
  return { tasks, edges };
}

/** raw → 图模型。纯函数；节点坐标由调用方随后 layoutNodes 覆写。 */
export function buildGraph(raw) {
  const { overview, roster, groups } = raw;
  const rosterRows = (roster && roster.roster) || [];
  const targets = (roster && roster.targets) || {};
  const regions = (overview && overview.regions) || [];

  const poolMembers = ((overview && overview.pool) || []).slice(0, POOL_LIMIT);

  const servicesByRegion = {};
  for (const group of groups || []) {
    servicesByRegion[group.region === null ? PLATFORM_KEY : group.region] =
      group.services || [];
  }

  const nextRegions = [];
  const nextTasks = [];
  const nextEdges = [];
  // 聚合区域从普通循环里摘出（最后单独成圆形聚合节点）
  const regionSpecs = regions
    .map((region) => ({ key: region.key, region }))
    .filter((spec) => !(spec.key in AGGREGATE_REGIONS));
  const platform = servicesByRegion[PLATFORM_KEY] || [];
  if (platform.length) {
    regionSpecs.push({ key: PLATFORM_KEY, region: null });
  }
  // 补集桶虚拟节点（组织树无成员但名册/目标存在时补出）
  if (!regions.some((r) => r.key === COMPLEMENT_REGION)
      && (rosterRows.some((row) => row.scope === COMPLEMENT_REGION
                                    && Number(row.enabled) === 1)
          || (targets[COMPLEMENT_REGION]))) {
    regionSpecs.push({ key: COMPLEMENT_REGION, region: null });
  }

  for (const spec of regionSpecs) {
    const key = spec.key;
    const isPlatform = key === PLATFORM_KEY;
    const isComplement = key === COMPLEMENT_REGION && !spec.region;
    // 部门/成员随节点携带（部门输入端口 + 成员输出口拉线的数据底）
    const depts = isPlatform || !spec.region ? [] : (spec.region.depts || [])
      .map((dept) => ({
        dept_id: dept.dept_id || '',
        dept_name: dept.dept_name || dept.label || '',
        memberCount: (dept.members || []).length,
      }));
    const members = isPlatform || !spec.region ? []
      : (spec.region.depts || []).flatMap((dept) =>
        (dept.members || []).map((m) => ({
          user_id: m.user_id,
          name: m.name,
          region: key,
          dept_id: dept.dept_id || '',
          dept_name: dept.dept_name || '',
        })));
    const memberCount = members.length;
    const rosterCount = isPlatform ? 0 : rosterRows
      .filter((row) => row.scope === key && Number(row.enabled) === 1).length;
    const targetMap = (targets[key]) || {};
    const targetCount = Object.keys(targetMap).length;
    const targetSum = Object.values(targetMap)
      .reduce((sum, value) => sum + (Number(value) || 0), 0);
    const services = servicesByRegion[key] || [];
    const badge = isPlatform
      ? { text: '平台线', cls: 'plain' }
      : isComplement
        ? { text: '补集', cls: 'plain' }
        : regionBadge(rosterCount, targetCount, services.length);
    nextRegions.push({
      id: `region:${key}`,
      key,
      label: isPlatform ? '平台 / 无群'
        : isComplement ? '其他（补集）'
        : (spec.region.label || key),
      badge,
      rosterCount,
      targetCount,
      targetSum,
      memberCount,
      depts,
      members,
      taskCount: services.length,
      isPlatform,
      x: REGION_X,
      y: TOP_Y,
    });
    // 家族模块化（2026-10-16）：同一区域下同家族任务折叠为模块节点——
    // 家族=模板、区域=实例（robot-hangzhou/robot-shaoxing 同属 robot-*）。
    // 连线 N:1 收敛为 区域→模块；状态聚合（全绿/有停用/有失败）。
    const { tasks, edges: familyEdges } = collectFamilyModules(key, services, { badge });
    nextTasks.push(...tasks);
    nextEdges.push(...familyEdges);
  }
  // 聚合节点：指标=组成桶求和；汇聚边 桶→聚合；下游任务照常成模块。
  for (const [aggKey, spec] of Object.entries(AGGREGATE_REGIONS)) {
    const services = servicesByRegion[aggKey] || [];
    const partNodes = nextRegions.filter((n) => spec.parts.includes(n.key));
    const agg = {
      id: `region:${aggKey}`,
      key: aggKey,
      label: spec.label,
      badge: { text: '聚合', cls: 'agg' },
      rosterCount: partNodes.reduce((s, n) => s + n.rosterCount, 0),
      targetCount: partNodes.reduce((s, n) => s + n.targetCount, 0),
      targetSum: partNodes.reduce((s, n) => s + n.targetSum, 0),
      memberCount: partNodes.reduce((s, n) => s + n.memberCount, 0),
      taskCount: services.length,
      isAggregate: true,
      parts: spec.parts,
      x: REGION_X,
      y: TOP_Y,
    };
    nextRegions.push(agg);
    for (const part of partNodes) {
      nextEdges.push({
        id: `cv:${part.key}:${aggKey}`,
        from: part.id,
        to: agg.id,
        broken: false,
        converge: true,
      });
    }
    const { tasks, edges: familyEdges } = collectFamilyModules(aggKey, services);
    nextTasks.push(...tasks);
    nextEdges.push(...familyEdges);
  }
  return {
    poolMembers,
    regionNodes: nextRegions,
    taskNodes: nextTasks,
    edges: nextEdges,
  };
}
