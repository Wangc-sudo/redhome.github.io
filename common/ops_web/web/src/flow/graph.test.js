/**
 * flow/graph.js 单测（2026-10-16 F1）：建图纯函数钉住——家族分组、聚合求和、
 * 补集/平台特例、徽标与 broken 语义。
 */
import { describe, expect, it } from 'vitest';
import {
  COMPLEMENT_REGION,
  PLATFORM_KEY,
  buildGraph,
  familyLabel,
  familyOf,
  fmt,
  regionBadge,
  taskBroken,
} from './graph.js';

function svc(id, { enabled = true, status = null, depends_on = [], ...rest } = {}) {
  return {
    service_id: id,
    service_label: id,
    enabled,
    category_label: '催办',
    latest: status ? { status } : null,
    depends_on,
    ...rest,
  };
}

function region(key, label, depts = []) {
  return { key, label, depts };
}

function raw({
  regions = [],
  pool = [],
  rosterRows = [],
  targets = {},
  groups = [],
} = {}) {
  return {
    overview: { regions, pool },
    roster: { roster: rosterRows, targets },
    groups,
  };
}

describe('familyOf', () => {
  it('剥掉区域后缀', () => {
    expect(familyOf('robot-hangzhou', 'hangzhou')).toBe('robot');
    expect(familyOf('robot-check-hangzhou', 'hangzhou')).toBe('robot-check');
  });
  it('无后缀保持原值', () => {
    expect(familyOf('extract-mart', 'hangzhou')).toBe('extract-mart');
  });
  it('平台线不剥后缀', () => {
    expect(familyOf('sync-dingtalk', PLATFORM_KEY)).toBe('sync-dingtalk');
  });
});

describe('familyLabel', () => {
  it('映射表优先', () => {
    expect(familyLabel('robot-check', null)).toBe('催办未填 + DING');
  });
  it('缺省用首成员类别标签', () => {
    expect(familyLabel('sync-x', svc('sync-x'))).toBe('催办·sync-x');
    expect(familyLabel('sync-x', null)).toBe('任务·sync-x');
  });
});

describe('taskBroken / regionBadge / fmt', () => {
  it('停用或最近失败即 broken', () => {
    expect(taskBroken(svc('a', { enabled: false }))).toBe(true);
    expect(taskBroken(svc('a', { status: 'failed' }))).toBe(true);
    expect(taskBroken(svc('a', { status: 'completed' }))).toBe(false);
    expect(taskBroken(svc('a'))).toBe(false);
  });
  it('徽标四态', () => {
    expect(regionBadge(0, 1, 1)).toEqual({ text: '缺名册', cls: 'warn' });
    expect(regionBadge(1, 0, 1)).toEqual({ text: '缺目标', cls: 'warn' });
    expect(regionBadge(1, 1, 0)).toEqual({ text: '待下游', cls: 'warn' });
    expect(regionBadge(1, 1, 1)).toEqual({ text: '已闭环', cls: 'ok' });
  });
  it('fmt 整数去小数', () => {
    expect(fmt(12)).toBe('12');
    expect(fmt(12.34)).toBe('12.3');
    expect(fmt('abc')).toBe('0');
  });
});

describe('buildGraph', () => {
  it('服务按家族折叠为模块节点 + 区域→模块边', () => {
    const g = buildGraph(raw({
      regions: [region('hangzhou', '杭州')],
      groups: [{
        region: 'hangzhou',
        services: [
          svc('robot-hangzhou'),
          svc('robot-check-hangzhou'),
          svc('pages-hangzhou'),
        ],
      }],
      rosterRows: [{ scope: 'hangzhou', enabled: 1, person_name: '张三' }],
      targets: { hangzhou: { 张三: 100 } },
    }));
    const modules = g.taskNodes.map((n) => n.family).sort();
    expect(modules).toEqual(['pages', 'robot', 'robot-check']);
    expect(g.taskNodes.every((n) => n.regionKey === 'hangzhou')).toBe(true);
    // 区域→模块 N:1 边（不含汇聚边——聚合节点恒创建，hangzhou 是其组成桶）
    const familyEdges = g.edges.filter((e) => !e.converge);
    expect(familyEdges.map((e) => e.id).sort()).toEqual([
      'e:hangzhou:pages', 'e:hangzhou:robot', 'e:hangzhou:robot-check',
    ]);
    expect(familyEdges.every((e) => e.from === 'region:hangzhou')).toBe(true);
    // 名册/目标计数
    const hz = g.regionNodes.find((n) => n.key === 'hangzhou');
    expect(hz.rosterCount).toBe(1);
    expect(hz.targetSum).toBe(100);
    expect(hz.badge).toEqual({ text: '已闭环', cls: 'ok' });
  });

  it('徽标 warn 或成员 broken 让边标红', () => {
    const g = buildGraph(raw({
      regions: [region('hangzhou', '杭州')],
      groups: [{ region: 'hangzhou', services: [svc('robot-hangzhou', { enabled: false })] }],
      rosterRows: [{ scope: 'hangzhou', enabled: 1, person_name: '张三' }],
      targets: { hangzhou: { 张三: 1 } },
    }));
    expect(g.edges[0].broken).toBe(true);
    expect(g.taskNodes[0].brokenCount).toBe(1);
  });

  it('聚合节点=组成桶求和 + 汇聚边', () => {
    const g = buildGraph(raw({
      regions: [
        region('hangzhou', '杭州'),
        region('shaoxing', '绍兴'),
        region('other', '其他区'),
      ],
      groups: [{ region: 'offline_all', services: [svc('leaderboard-offline_all')] }],
      rosterRows: [
        { scope: 'hangzhou', enabled: 1, person_name: 'a' },
        { scope: 'shaoxing', enabled: 1, person_name: 'b' },
        { scope: 'shaoxing', enabled: 0, person_name: '停用不计' },
      ],
      targets: { hangzhou: { a: 100 }, shaoxing: { b: 50 } },
    }));
    const agg = g.regionNodes.find((n) => n.key === 'offline_all');
    expect(agg.isAggregate).toBe(true);
    expect(agg.label).toBe('线下整体');
    expect(agg.rosterCount).toBe(2);      // 杭1+绍1（停用不计）
    expect(agg.targetSum).toBe(150);
    // 汇聚边：parts 中有节点的桶 → 聚合（杭/绍/其他三桶都在）
    const cv = g.edges.filter((e) => e.converge);
    expect(cv.map((e) => e.id).sort()).toEqual([
      'cv:hangzhou:offline_all', 'cv:other:offline_all', 'cv:shaoxing:offline_all',
    ]);
    expect(cv.every((e) => e.broken === false)).toBe(true);
    // 聚合下游照常成模块，边 broken 不带徽标语义（只认成员 broken）
    const aggEdge = g.edges.find((e) => e.id === 'e:offline_all:leaderboard');
    expect(aggEdge.broken).toBe(false);
    expect(g.taskNodes.find((n) => n.regionKey === 'offline_all').family)
      .toBe('leaderboard');
  });

  it('补集桶虚拟节点：名册有启用行即补出', () => {
    const g = buildGraph(raw({
      regions: [region('hangzhou', '杭州')],
      rosterRows: [{ scope: COMPLEMENT_REGION, enabled: 1, person_name: '余云涛' }],
    }));
    const other = g.regionNodes.find((n) => n.key === COMPLEMENT_REGION);
    expect(other).toBeTruthy();
    expect(other.label).toBe('其他（补集）');
    expect(other.badge).toEqual({ text: '补集', cls: 'plain' });
    expect(other.rosterCount).toBe(1);
  });

  it('补集桶：组织树已含该区域则不虚拟补出', () => {
    const g = buildGraph(raw({
      regions: [region(COMPLEMENT_REGION, '其他区')],
    }));
    const nodes = g.regionNodes.filter((n) => n.key === COMPLEMENT_REGION);
    expect(nodes).toHaveLength(1);
    expect(nodes[0].label).toBe('其他区');
  });

  it('平台线：region=null 的服务组挂到「平台 / 无群」节点', () => {
    const g = buildGraph(raw({
      groups: [{ region: null, services: [svc('extract-mart')] }],
    }));
    const platform = g.regionNodes.find((n) => n.isPlatform);
    expect(platform.label).toBe('平台 / 无群');
    expect(platform.badge).toEqual({ text: '平台线', cls: 'plain' });
    expect(g.taskNodes[0].family).toBe('extract-mart');   // 不剥后缀
  });

  it('成员/部门随区域节点携带；池截断 50', () => {
    const depts = [{
      dept_id: 'd1',
      dept_name: '余杭',
      members: [{ user_id: 'u1', name: '张三' }],
    }];
    const g = buildGraph(raw({
      regions: [region('hangzhou', '杭州', depts)],
      pool: Array.from({ length: 60 }, (_, i) => ({ user_id: `p${i}` })),
    }));
    const hz = g.regionNodes[0];
    expect(hz.depts).toEqual([{ dept_id: 'd1', dept_name: '余杭', memberCount: 1 }]);
    expect(hz.members).toEqual([{
      user_id: 'u1', name: '张三', region: 'hangzhou', dept_id: 'd1', dept_name: '余杭',
    }]);
    expect(hz.memberCount).toBe(1);
    expect(g.poolMembers).toHaveLength(50);
  });

  it('空输入不炸（聚合节点恒创建、指标全零）', () => {
    const g = buildGraph(raw());
    // 历史行为：AGGREGATE_REGIONS 无条件成节点，parts 不存在时零汇聚边
    expect(g.regionNodes.map((n) => n.key)).toEqual(['offline_all']);
    expect(g.regionNodes[0]).toMatchObject({
      rosterCount: 0, targetCount: 0, targetSum: 0, memberCount: 0,
    });
    expect(g.taskNodes).toEqual([]);
    expect(g.edges).toEqual([]);
    expect(g.poolMembers).toEqual([]);
  });
});
