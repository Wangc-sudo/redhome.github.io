/**
 * flow/layout.js 单测（2026-10-16 F1）：布局堆叠、动态高度、边路径生成器、
 * 依赖线聚合、画布尺寸——钉住与重构前逐像素一致的行为。
 */
import { describe, expect, it } from 'vitest';
import {
  AGG_SIZE,
  DEPT_ROW_H,
  GLOBAL_H,
  MEMBER_ROW_H,
  REGION_W,
  REGION_X,
  TASK_GAP,
  TASK_W,
  TASK_X,
  TOP_Y,
  buildDepEdges,
  buildEdgePaths,
  canvasSize,
  edgePath,
  layoutNodes,
  moduleH,
  nodeRects,
  regionH,
} from './layout.js';

const NO_EXPAND = new Set();

function regionNode(key, { depts = [], members = [], ...rest } = {}) {
  return {
    id: `region:${key}`, key, depts, members, x: 0, y: 0, ...rest,
  };
}

function taskNode(key, family, services) {
  return {
    id: `module:${key}:${family}`, regionKey: key, family,
    label: family, services, x: 0, y: 0,
  };
}

describe('regionH / moduleH', () => {
  it('聚合=圆径；平台=固定 120', () => {
    expect(regionH(regionNode('a', { isAggregate: true }), NO_EXPAND)).toBe(AGG_SIZE);
    expect(regionH(regionNode('a', { isPlatform: true }), NO_EXPAND)).toBe(120);
  });
  it('部门行 + 展开成员行计入高度', () => {
    const node = regionNode('hangzhou', {
      depts: [{}, {}],
      members: [{}, {}, {}],
    });
    const collapsed = regionH(node, NO_EXPAND);
    expect(collapsed).toBe(34 + 52 + 30 + 10 + 22 + 2 * DEPT_ROW_H);
    const expanded = regionH(node, new Set([node.id]));
    expect(expanded).toBe(collapsed + 24 + 3 * MEMBER_ROW_H);
  });
  it('模块折叠 64，展开 44+78n', () => {
    const mod = taskNode('hangzhou', 'robot', [{}, {}]);
    expect(moduleH(mod, NO_EXPAND)).toBe(64);
    expect(moduleH(mod, new Set([mod.id]))).toBe(44 + 2 * 78);
  });
});

describe('layoutNodes', () => {
  it('模块栈纵向堆叠，区域相对栈居中', () => {
    const region = regionNode('hangzhou', { depts: [{}] });
    const t1 = taskNode('hangzhou', 'robot', [{}]);
    const t2 = taskNode('hangzhou', 'pages', [{}]);
    const pool = layoutNodes({
      regionNodes: [region],
      taskNodes: [t1, t2],
      poolMembers: [],
      globalNodes: [],
    });
    expect(pool).toEqual({ id: 'pool', x: 40, y: TOP_Y });
    expect(t1).toMatchObject({ x: TASK_X, y: TOP_Y });
    expect(t2).toMatchObject({ x: TASK_X, y: TOP_Y + 64 + TASK_GAP });
    const stackH = 64 + TASK_GAP + 64;
    const rh = regionH(region, NO_EXPAND);
    expect(region.x).toBe(REGION_X);
    expect(region.y).toBe(Math.round(TOP_Y + Math.max(0, (stackH - rh) / 2)));
  });

  it('无模块区域：y 推进自身高度 + 26 间距', () => {
    const r1 = regionNode('a');
    const r2 = regionNode('b');
    layoutNodes({
      regionNodes: [r1, r2], taskNodes: [], poolMembers: [], globalNodes: [],
    });
    const rh = regionH(r1, NO_EXPAND);
    expect(r1.y).toBe(TOP_Y);
    expect(r2.y).toBe(TOP_Y + rh + 26);
  });

  it('savedPos 命中的节点保持拖动位置', () => {
    const region = regionNode('hangzhou');
    const task = taskNode('hangzhou', 'robot', [{}]);
    const globalNode = { id: 'global:calendar', x: 0, y: 0 };
    const pool = layoutNodes({
      regionNodes: [region],
      taskNodes: [task],
      poolMembers: [{}],
      globalNodes: [globalNode],
      savedPos: {
        'pool': { x: 9, y: 9 },
        'region:hangzhou': { x: 400, y: 300 },
        'module:hangzhou:robot': { x: 900, y: 500 },
        'global:calendar': { x: 60, y: 700 },
      },
    });
    expect(pool).toEqual({ id: 'pool', x: 9, y: 9 });
    expect(region).toMatchObject({ x: 400, y: 300 });
    expect(task).toMatchObject({ x: 900, y: 500 });
    expect(globalNode).toMatchObject({ x: 60, y: 700 });
  });

  it('全局节点排在池下方', () => {
    const g1 = { id: 'global:a', x: 0, y: 0 };
    const g2 = { id: 'global:b', x: 0, y: 0 };
    layoutNodes({
      regionNodes: [], taskNodes: [], poolMembers: [{}, {}],
      globalNodes: [g1, g2],
    });
    const poolH = 44 + 2 * 30 + 12;
    expect(g1).toMatchObject({ x: 40, y: poolH + TOP_Y + 28 });
    expect(g2.y).toBe(g1.y + GLOBAL_H + 16);
  });
});

describe('edgePath（统一生成器）', () => {
  it('缺省 abs=true：目标在左时控制点仍按间距张开', () => {
    expect(edgePath(100, 10, 300, 50))
      .toBe('M 100 10 C 200 10, 200 50, 300 50');
    // 反向（目标在源左侧）：abs 语义 dx=|x2-x1|/2
    expect(edgePath(300, 10, 100, 50))
      .toBe('M 300 10 C 400 10, 0 50, 100 50');
  });
  it('abs=false 保留历史有符号语义（区域→模块边）', () => {
    // 目标在左：旧实现 dx=max(60, (x2-x1)/2)=60
    expect(edgePath(300, 10, 100, 50, { abs: false }))
      .toBe('M 300 10 C 360 10, 40 50, 100 50');
  });
  it('间距小于 slack 时按 slack 张开', () => {
    expect(edgePath(0, 0, 50, 0)).toBe('M 0 0 C 60 0, -10 0, 50 0');
  });
});

describe('buildEdgePaths', () => {
  it('源右缘中点 → 目标左缘 y+40', () => {
    const edges = [{ id: 'e:a:f', from: 'region:a', to: 'module:a:f' }];
    const rects = {
      'region:a': { x: REGION_X, y: 100, w: REGION_W, h: 200 },
      'module:a:f': { x: TASK_X, y: 90, w: TASK_W, h: 64 },
    };
    const [p] = buildEdgePaths(edges, rects);
    expect(p.d).toBe(edgePath(REGION_X + REGION_W, 200, TASK_X, 130, { abs: false }));
  });
  it('缺矩形时 d 为空串', () => {
    const [p] = buildEdgePaths([{ id: 'x', from: 'no', to: 'none' }], {});
    expect(p.d).toBe('');
  });
});

describe('buildDepEdges', () => {
  const services = {
    a: [{ service_id: 'pages-hangzhou', depends_on: ['extract-mart'] }],
    b: [{ service_id: 'extract-mart', depends_on: [] }],
  };
  const mods = [
    taskNode('hangzhou', 'pages', services.a),
    taskNode('__platform__', 'extract-mart', services.b),
  ];
  mods[0].x = TASK_X; mods[0].y = 40;
  mods[1].x = TASK_X; mods[1].y = 300;

  it('成员 depends_on 聚合到模块层，源服务 ID 全带上', () => {
    const rects = nodeRects([], mods, NO_EXPAND, NO_EXPAND);
    const [dep] = buildDepEdges(mods, rects);
    expect(dep.id).toBe('module:__platform__:extract-mart→module:hangzhou:pages');
    expect(dep.fromLabel).toBe('extract-mart');
    expect(dep.toLabel).toBe('pages');
    expect(dep.sourceIds).toEqual(['extract-mart']);
    expect(dep.d).toBe(edgePath(TASK_X + TASK_W, 300 + 32, TASK_X, 40 + 34));
  });
  it('自依赖与重复依赖去重', () => {
    const dup = [
      taskNode('hangzhou', 'pages', [{
        service_id: 'pages-hangzhou',
        depends_on: ['pages-hangzhou', 'extract-mart', 'extract-mart'],
      }]),
      taskNode('__platform__', 'extract-mart', services.b),
    ];
    dup[0].x = TASK_X; dup[0].y = 40;
    dup[1].x = TASK_X; dup[1].y = 300;
    const rects = nodeRects([], dup, NO_EXPAND, NO_EXPAND);
    const deps = buildDepEdges(dup, rects);
    expect(deps).toHaveLength(1);   // 自依赖排除 + 重复收敛
  });
  it('依赖未知服务不产生边', () => {
    const orphan = [taskNode('hangzhou', 'pages', [{
      service_id: 'pages-hangzhou', depends_on: ['ghost'],
    }])];
    orphan[0].x = TASK_X; orphan[0].y = 40;
    const rects = nodeRects([], orphan, NO_EXPAND, NO_EXPAND);
    expect(buildDepEdges(orphan, rects)).toEqual([]);
  });
});

describe('nodeRects / canvasSize', () => {
  it('聚合=正方形，区域高按展开态', () => {
    const agg = regionNode('offline_all', { isAggregate: true });
    const biz = regionNode('hangzhou', { depts: [{}] });
    const rects = nodeRects([agg, biz], [], NO_EXPAND, NO_EXPAND);
    expect(rects['region:offline_all']).toMatchObject({ w: AGG_SIZE, h: AGG_SIZE });
    expect(rects['region:hangzhou'].h).toBe(regionH(biz, NO_EXPAND));
  });
  it('画布尺寸=节点外接+60 余量，下限 1300×480', () => {
    expect(canvasSize({}, [])).toEqual({ w: 1300, h: 480 });
    const rects = { a: { x: 2000, y: 900, w: 100, h: 50 } };
    expect(canvasSize(rects, [{ y: 1000 }])).toEqual({
      w: 2000 + 100 + 60,
      h: Math.max(900 + 50 + 60, 1000 + GLOBAL_H + 60),
    });
  });
});
