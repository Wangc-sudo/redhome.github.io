/**
 * flow/graph.js 单测（2026-10-16 F1）：建图纯函数钉住——家族分组、聚合求和、
 * 补集/平台特例、徽标与 broken 语义。
 */
import { describe, expect, it } from 'vitest';
import {
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
  completed = {},
  groups = [],
} = {}) {
  return {
    overview: { regions, pool },
    roster: { roster: rosterRows, targets, completed },
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
    // 播报收拢（2026-10-24 用户裁决）：robot/robot-check/pages 三家族
    // 合并为一张「日报任务」主卡，子线在成员行各自开关。
    const modules = g.taskNodes.map((n) => n.family);
    expect(modules).toEqual(['daily-report']);
    expect(g.taskNodes.every((n) => n.regionKey === 'hangzhou')).toBe(true);
    // 主卡成员=三条子线全在
    expect(g.taskNodes[0].services.map((s) => s.service_id).sort())
      .toEqual(['pages-hangzhou', 'robot-check-hangzhou', 'robot-hangzhou']);
    expect(g.taskNodes[0].label).toBe('日报任务');
    // 区域→模块边收敛为一条（不含汇聚边与日历消费边）
    const familyEdges = g.edges.filter((e) => !e.converge && !e.consumes);
    expect(familyEdges.map((e) => e.id)).toEqual(['e:hangzhou:daily-report']);
    expect(familyEdges.every((e) => e.from === 'region:hangzhou')).toBe(true);
    // 日历消费边：聚合主卡挂 global:calendar
    const consumeEdges = g.edges.filter((e) => e.consumes);
    expect(consumeEdges.map((e) => e.to)).toEqual(['module:hangzhou:daily-report']);
    expect(consumeEdges.every((e) => e.from === 'global:calendar')).toBe(true);
    // 名册/目标计数
    const hz = g.regionNodes.find((n) => n.key === 'hangzhou');
    expect(hz.rosterCount).toBe(1);
    expect(hz.targetSum).toBe(100);
    expect(hz.badge).toEqual({ text: '已闭环', cls: 'ok' });
  });

  it('播报收拢不波及平台线家族', () => {
    // 平台线（region=null）的同名家 services 保持各自家族模块，
    // 不收进 daily-report（采集/加工泳道的流水线语义不变）。
    const g = buildGraph(raw({
      groups: [{
        region: null,
        services: [svc('sync-dingtalk'), svc('extract-mart')],
      }],
    }));
    const families = g.taskNodes.map((n) => n.family).sort();
    expect(families).toEqual(['extract-mart', 'sync-dingtalk']);
  });

  it('业务线分组排序（2026-10-24 面板按 sheet 切割）', () => {
    // 输入乱序 → 输出按 BUSINESS_GROUPS 组序+组内声明序
    const g = buildGraph(raw({
      regions: [
        region('hq', '总部'),
        region('vanke', '体验馆与团购'),
        region('hangzhou', '杭州'),
        region('shaoxing', '绍兴'),
        region('province_ops', '省外与运营中心'),
        region('qudao', '渠道'),
      ],
      groups: [],
      rosterRows: [
        { scope: 'hangzhou', enabled: 1, person_name: 'a' },
        { scope: 'shaoxing', enabled: 1, person_name: 'b' },
      ],
      targets: { hangzhou: { a: 1 }, shaoxing: { b: 1 } },
    }));
    const keys = g.regionNodes.map((n) => n.key);
    // 线下组（杭/绍/省外运营中心，2026-10-26 裁决合一）→ 体验馆团购 →
    // 总部；聚合线下整体随第一桶组（offline）。渠道已摘除不进画布。
    expect(keys).not.toContain('qudao');
    expect(keys.indexOf('hangzhou')).toBeLessThan(keys.indexOf('shaoxing'));
    expect(keys.indexOf('shaoxing')).toBeLessThan(keys.indexOf('province_ops'));
    expect(keys.indexOf('province_ops')).toBeLessThan(keys.indexOf('vanke'));
    expect(keys.indexOf('vanke')).toBeLessThan(keys.indexOf('hq'));
  });

  it('徽章口径：总部职能底不警告', () => {
    const g = buildGraph(raw({ regions: [region('hq', '总部')] }));
    const hq = g.regionNodes.find((n) => n.key === 'hq');
    expect(hq.badge).toEqual({ text: '职能底', cls: 'plain' });
  });

  it('未上线区域不进画布（2026-10-26 裁决：渠道/餐饮/君品/线下额外/其他）', () => {
    // 渠道店铺级徽章特判保留备用（上线时随 HIDDEN_REGION_KEYS 移除恢复）。
    const g = buildGraph(raw({
      regions: [
        region('qudao', '渠道'),
        region('dining', '餐饮'),
        region('junpin', '君品雅院'),
        region('offline_extra', '线下额外'),
        region('other', '其他'),
        region('hangzhou', '杭州'),
      ],
      groups: [{ region: 'qudao', services: [svc('channel-missing-check')] }],
      rosterRows: [
        { scope: 'qudao', enabled: 1, entity_type: 'store', entity_key: 'PDD店' },
      ],
      targets: {},
    }));
    const keys = g.regionNodes.map((n) => n.key);
    for (const hidden of ['qudao', 'dining', 'junpin', 'offline_extra', 'other']) {
      expect(keys).not.toContain(hidden);
    }
    expect(keys).toContain('hangzhou');
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
    // 2026-10-26 裁决口径：线下整体=杭/绍/省外运营中心三桶；其他（补集）
    // 即使在册也不再参与汇聚。
    const g = buildGraph(raw({
      regions: [
        region('hangzhou', '杭州'),
        region('shaoxing', '绍兴'),
        region('province_ops', '省外与运营中心'),
        region('other', '其他区'),
      ],
      groups: [{ region: 'offline_all', services: [svc('leaderboard-offline_all')] }],
      rosterRows: [
        { scope: 'hangzhou', enabled: 1, person_name: 'a' },
        { scope: 'shaoxing', enabled: 1, person_name: 'b' },
        { scope: 'shaoxing', enabled: 0, person_name: '停用不计' },
        { scope: 'province_ops', enabled: 1, person_name: 'c' },
        { scope: 'other', enabled: 1, person_name: '补集不计入' },
      ],
      targets: {
        hangzhou: { a: 100 }, shaoxing: { b: 50 }, province_ops: { c: 25 },
      },
    }));
    const agg = g.regionNodes.find((n) => n.key === 'offline_all');
    expect(agg.isAggregate).toBe(true);
    expect(agg.label).toBe('线下整体');
    expect(agg.rosterCount).toBe(3);      // 杭1+绍1+省外1（停用与补集不计）
    expect(agg.targetSum).toBe(175);
    // 汇聚边：parts 中有节点的桶 → 聚合（杭/绍/省外三桶；其他不再汇聚）
    const cv = g.edges.filter((e) => e.converge);
    expect(cv.map((e) => e.id).sort()).toEqual([
      'cv:hangzhou:offline_all', 'cv:province_ops:offline_all', 'cv:shaoxing:offline_all',
    ]);
    expect(cv.every((e) => e.broken === false)).toBe(true);
    // 聚合下游照常成模块（播报收拢后为 daily-report 主卡），边 broken
    // 不带徽标语义（只认成员 broken）
    const aggEdge = g.edges.find((e) => e.id === 'e:offline_all:daily-report');
    expect(aggEdge.broken).toBe(false);
    expect(g.taskNodes.find((n) => n.regionKey === 'offline_all').family)
      .toBe('daily-report');
  });

  it('完成率=当月完成额(元)/目标(万元)，目标 0 → null；聚合按桶求和换算', () => {
    // 2026-10-26「播报回显完成百分比上面板」：与播报回执同口径。
    const g = buildGraph(raw({
      regions: [region('hangzhou', '杭州'), region('shaoxing', '绍兴')],
      targets: { hangzhou: { a: 100 }, shaoxing: {} },  // 万元
      completed: { hangzhou: 500000 },                  // 元 → 50 万 → 50%
    }));
    const hz = g.regionNodes.find((n) => n.key === 'hangzhou');
    expect(hz.completedSum).toBe(500000);
    expect(hz.completionRate).toBeCloseTo(0.5);
    // 目标 0/无 → null（显示「—」，与榜单「目标 0 不参与」同语义）
    expect(g.regionNodes.find((n) => n.key === 'shaoxing').completionRate)
      .toBeNull();
    // 聚合（offline_all parts=hangzhou/shaoxing/province_ops）：求和后换算
    const agg = g.regionNodes.find((n) => n.key === 'offline_all');
    expect(agg.completedSum).toBe(500000);
    expect(agg.completionRate).toBeCloseTo(0.5);
  });

  it('补集桶机制已退役（2026-10-26）：other 注册行被 HIDDEN 过滤', () => {
    // other 已是 dim_org_region 注册区域（96 人归编兜底），用户裁决摘除；
    // 即使名册/目标再现 other 行也不再补出虚拟节点。
    const g = buildGraph(raw({
      regions: [region('hangzhou', '杭州'), region('other', '其他')],
      rosterRows: [{ scope: 'other', enabled: 1, person_name: '某人' }],
      targets: { other: { 某人: 1 } },
    }));
    const keys = g.regionNodes.map((n) => n.key);
    expect(keys).not.toContain('other');
    expect(keys).toContain('hangzhou');
  });

  it('测试区不进区域层（2026-10-19 改制为「播报试投」功能模块）', () => {
    const g = buildGraph(raw({
      regions: [region('hangzhou', '杭州'), region('test', '测试区')],
      groups: [{ region: 'test', services: [svc('robot-test')] }],
    }));
    expect(g.regionNodes.find((n) => n.key === 'test')).toBeUndefined();
    expect(g.regionNodes.find((n) => n.key === 'hangzhou')).toBeTruthy();
  });

  it('平台线：region=null 的服务散落加工泳道模块（流水线分层，2026-10-17）', () => {
    const g = buildGraph(raw({
      groups: [{ region: null, services: [svc('extract-mart')] }],
    }));
    // 不再有「平台/无群」区域节点；服务进加工泳道模块
    expect(g.regionNodes.find((n) => n.isPlatform)).toBeUndefined();
    expect(g.taskNodes[0].family).toBe('extract-mart');   // 不剥后缀
    expect(g.taskNodes[0].stage).toBe('process');
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
