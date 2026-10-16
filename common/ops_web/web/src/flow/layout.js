/**
 * 闭环画布·布局与连线几何纯函数（2026-10-16 F1 从 FlowPage.vue 抽出，行为零变化）。
 * 零依赖（不 import vue）：展开态/持久化坐标一律以参数传入——可单测。
 * edgePath 是全部连线路径的唯一收口点（F5 多方向拉线在此扩展 side 参数）。
 */

// -- 布局常量 ------------------------------------------------------------------
export const POOL_W = 220;
export const POOL_ROW_H = 30;
export const POOL_HEAD_H = 44;
export const REGION_W = 300;
export const TASK_W = 330;
export const TASK_GAP = 14;
export const REGION_X = 330;
export const TASK_X = 850;
export const TOP_Y = 40;
export const DEPT_ROW_H = 26;
export const MEMBER_ROW_H = 26;
export const GLOBAL_W = 220;
export const GLOBAL_H = 64;
export const AGG_SIZE = 176;   // 聚合节点圆形直径
export const POOL_X = 40;      // 左列（池/全局节点）默认 x

/** 区域节点动态高度：头+指标+脚 + 部门行 +（展开时）成员行。 */
export function regionH(node, expandedRegions) {
  if (node.isAggregate) return AGG_SIZE;
  if (node.isPlatform) return 120;
  let h = 34 + 52 + 30 + 10;                     // head+metrics+foot+padding
  h += 22 + (node.depts ? node.depts.length : 0) * DEPT_ROW_H;   // 部门槽
  if (expandedRegions && expandedRegions.has(node.id)) {
    h += 24 + (node.members ? node.members.length : 0) * MEMBER_ROW_H;
  }
  return h;
}

/** 模块节点高度（折叠 64；展开按成员行数）。 */
export function moduleH(node, expandedModules) {
  const expanded = expandedModules && expandedModules.has(node.id);
  return expanded ? 44 + node.services.length * 78 : 64;
}

/**
 * 自动布局：池/全局节点在左列；每区域的模块栈纵向堆叠、区域节点相对栈居中。
 * savedPos 命中的节点保持用户拖动位置。直接改写传入节点的 x/y、
 * 返回池节点对象（与历史行为一致）。
 */
export function layoutNodes({
  regionNodes,
  taskNodes,
  poolMembers,
  globalNodes,
  savedPos = {},
  expandedRegions,
  expandedModules,
}) {
  let cursorY = TOP_Y;
  const poolH = POOL_HEAD_H + poolMembers.length * POOL_ROW_H + 12;
  const savedPool = savedPos.pool;
  const poolNode = {
    id: 'pool',
    x: savedPool ? savedPool.x : POOL_X,
    y: savedPool ? savedPool.y : TOP_Y,
  };
  // 全局节点：池下方纵向排列（可被拖动覆盖）
  const poolBottom = poolH + poolNode.y + 28;
  globalNodes.forEach((node, index) => {
    const saved = savedPos[node.id];
    node.x = saved ? saved.x : POOL_X;
    node.y = saved ? saved.y : poolBottom + index * (GLOBAL_H + 16);
  });
  for (const region of regionNodes) {
    const stack = taskNodes.filter((t) => t.regionKey === region.key);
    const stackTop = cursorY;
    for (const task of stack) {
      const saved = savedPos[task.id];
      task.x = saved ? saved.x : TASK_X;
      task.y = saved ? saved.y : cursorY;
      cursorY += moduleH(task, expandedModules) + TASK_GAP;
    }
    const stackH = cursorY - stackTop - TASK_GAP;
    const rh = regionH(region, expandedRegions);
    const regionY = stack.length
      ? stackTop + Math.max(0, (stackH - rh) / 2)
      : cursorY;
    if (!stack.length) cursorY += rh;
    cursorY += 26;
    const saved = savedPos[region.id];
    region.x = saved ? saved.x : REGION_X;
    region.y = saved ? saved.y : Math.round(regionY);
  }
  return poolNode;
}

// -- 连线几何 -------------------------------------------------------------------

/** 节点矩形表（连线端点取位用）：聚合=圆（边长 AGG_SIZE），区域高按展开态。 */
export function nodeRects(regionNodes, taskNodes, expandedRegions, expandedModules) {
  const map = {};
  for (const n of regionNodes) {
    map[n.id] = n.isAggregate
      ? { ...n, w: AGG_SIZE, h: AGG_SIZE }
      : { ...n, w: REGION_W, h: regionH(n, expandedRegions) };
  }
  for (const n of taskNodes) {
    map[n.id] = { ...n, w: TASK_W, h: moduleH(n, expandedModules) };
  }
  return map;
}

/**
 * 统一边路径生成器（F5 多方向拉线的唯一收口点）。
 * 现状=水平贝塞尔：控制点沿 ±x 延伸 dx。
 * abs=false 保留区域→模块边的历史有符号语义（目标在源左侧时 dx 恒退化为
 * slack，形态与旧实现逐像素一致）；abs=true（缺省）用于依赖线/草稿线。
 */
export function edgePath(x1, y1, x2, y2, { abs = true, slack = 60 } = {}) {
  const dx = Math.max(slack, (abs ? Math.abs(x2 - x1) : (x2 - x1)) / 2);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

/** 区域→模块边路径（端点：源右缘中点 → 目标左缘 y+40）。 */
export function buildEdgePaths(edges, rects) {
  return edges.map((edge) => {
    const from = rects[edge.from];
    const to = rects[edge.to];
    if (!from || !to) return { ...edge, d: '' };
    return {
      ...edge,
      d: edgePath(from.x + from.w, from.y + from.h / 2, to.x, to.y + 40,
        { abs: false }),
    };
  });
}

/** 依赖线（模块→模块）：成员服务 depends_on 聚合到家族模块层。 */
export function buildDepEdges(taskNodes, rects) {
  const moduleOfService = {};
  for (const n of taskNodes) {
    for (const s of n.services) moduleOfService[s.service_id] = n;
  }
  const seen = new Set();
  const out = [];
  for (const n of taskNodes) {
    const sourceIds = new Set();
    for (const s of n.services) {
      for (const dep of s.depends_on || []) {
        const from = moduleOfService[dep];
        if (from && from.id !== n.id) sourceIds.add(from.id);
      }
    }
    for (const fromId of sourceIds) {
      const key = `${fromId}→${n.id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const from = rects[fromId];
      const to = rects[n.id];
      if (!from || !to) continue;
      const fromModule = taskNodes.find((m) => m.id === fromId);
      out.push({
        id: key,
        fromLabel: fromModule.label,
        toLabel: n.label,
        fromModule,
        toModule: n,
        sourceIds: fromModule.services.map((s) => s.service_id),
        d: edgePath(from.x + from.w, from.y + from.h / 2, to.x, to.y + 34),
      });
    }
  }
  return out;
}

/** 画布滚动面尺寸：节点外接矩形 + 60 余量，下限 1300×480。 */
export function canvasSize(rects, globalNodes) {
  let maxX = 1300;
  let maxY = 480;
  for (const n of Object.values(rects)) {
    maxX = Math.max(maxX, n.x + n.w + 60);
    maxY = Math.max(maxY, n.y + n.h + 60);
  }
  for (const n of globalNodes) {
    maxY = Math.max(maxY, n.y + GLOBAL_H + 60);
  }
  return { w: maxX, h: maxY };
}
