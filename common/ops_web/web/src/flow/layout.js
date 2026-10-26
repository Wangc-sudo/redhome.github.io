/**
 * 闭环画布·布局与连线几何纯函数（2026-10-16 F1 从 FlowPage.vue 抽出；
 * 2026-10-17 F5 多方向拉线落地）。
 * 零依赖（不 import vue）：展开态/持久化坐标一律以参数传入——可单测。
 * 连线唯一收口：edgePathSides（方向自适应贝塞尔）+ pickSides/portAnchor
 * 方位选择 + draftPath（拉线草稿）；F1 的 edgePath（水平钉死）已退役。
 */

// -- 布局常量 ------------------------------------------------------------------
export const POOL_W = 220;
export const POOL_ROW_H = 30;
export const POOL_HEAD_H = 44;
export const REGION_W = 300;
export const TASK_W = 330;
export const TASK_GAP = 14;
export const TOP_Y = 40;
export const DEPT_ROW_H = 26;
export const MEMBER_ROW_H = 26;
export const GLOBAL_W = 220;
export const GLOBAL_H = 64;
export const AGG_SIZE = 176;   // 聚合节点圆形直径
export const POOL_X = 40;      // 左列（池/全局节点）默认 x

// -- 流水线泳道（2026-10-17 五层流水线改版）：源 → 采集 → 加工 → 区域 → 应用 --
export const LANE_X = {
  source: 40,
  collect: 300,
  process: 700,
  region: 1110,
  app: 1490,
};
export const LANE_W = {
  source: 200,
  collect: TASK_W,
  process: TASK_W,
  region: REGION_W,
  app: TASK_W,
};
export const REGION_X = LANE_X.region;
export const TASK_X = LANE_X.app;

// -- 业务线分组（2026-10-24 用户裁决「面板按 sheet 切割」）---------------------
//: 画布区域层按权威源 Excel 的 sheet 目录分组展示，顺序即 sheet 顺序。
//: 未列入的注册区域落「未分组」段尾部。muted 组（总部职能）不报数，
//: 只展示不亮警告徽章。放 layout.js 保持 graph→layout 单向依赖。
//: 2026-10-26 用户裁决：线下网点=杭州+绍兴+省外与运营中心（province_ops
//: 独立组撤销并入）；other 补集桶出组（名册/目标已迁 province_ops，若
//: 再现数据落「未分组」段尾部）。
//: 2026-10-26：qudao（线上渠道）/dining（餐饮，含 junpin）两组随区域
//: 从画布摘除而下线（graph.js HIDDEN_REGION_KEYS）；上线时组与区域
//: 一并恢复。
export const BUSINESS_GROUPS = [
  { key: 'offline', label: '线下网点', regions: ['hangzhou', 'shaoxing', 'province_ops'] },
  { key: 'vanke', label: '体验馆与团购', regions: ['vanke'] },
  { key: 'hq', label: '总部职能', regions: ['hq'], muted: true },
];

//: region_key → 分组（未注册进任何组的区域返回 null=未分组段）。
export function groupOf(regionKey) {
  for (const group of BUSINESS_GROUPS) {
    if (group.regions.includes(regionKey)) return group;
  }
  return null;
}

//: 区域排序键：组序 → 组内 regions 声明序 → 未分组按字典序殿后。
export function regionSortKey(regionKey) {
  const group = groupOf(regionKey);
  if (!group) return `${BUSINESS_GROUPS.length}:zz:${regionKey}`;
  return `${BUSINESS_GROUPS.indexOf(group)}:${group.regions.indexOf(regionKey)}`;
}

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
 * 自动布局（2026-10-17 顶部功能条改版）：
 * **功能模块横排成顶部条带**（池 + 全局节点左起横排），区域列从条带下方
 * 开始（居中列），模块栈在右列纵向堆叠、区域节点相对栈居中。
 * savedPos 命中的节点保持用户拖动位置。直接改写传入节点的 x/y、
 * 返回池节点对象（与历史行为一致）。
 */
export function layoutNodes({
  regionNodes,
  taskNodes,
  poolMembers,
  globalNodes,
  sourceNodes = [],
  savedPos = {},
  expandedRegions,
  expandedModules,
}) {
  // -- 顶部功能条：池 + 全局节点横排 ------------------------------------
  const poolH = POOL_HEAD_H + poolMembers.length * POOL_ROW_H + 12;
  const savedPool = savedPos.pool;
  const poolNode = {
    id: 'pool',
    x: savedPool ? savedPool.x : POOL_X,
    y: savedPool ? savedPool.y : TOP_Y,
  };
  let stripX = poolNode.x + POOL_W + 24;
  const stripY = poolNode.y;
  globalNodes.forEach((node) => {
    const saved = savedPos[node.id];
    node.x = saved ? saved.x : stripX;
    node.y = saved ? saved.y : stripY;
    if (!saved) stripX += GLOBAL_W + 24;
  });
  const stripBottom = stripY + Math.max(poolH, GLOBAL_H) + 40;

  // -- 流水线泳道（2026-10-17）：源/采集/加工层纵向堆叠，区域/应用照旧 -------
  // 源节点与采集/加工模块从条带下方开始按泳道堆叠。
  const laneCursor = {};
  const stackLane = (stage, nodes, w) => {
    let y = laneCursor[stage] || (stripBottom + 26);
    for (const n of nodes) {
      const saved = savedPos[n.id];
      n.x = saved ? saved.x : LANE_X[stage];
      n.y = saved ? saved.y : y;
      const h = stage === 'source' ? 56 : moduleH(n, expandedModules);
      if (!saved) y += h + TASK_GAP;
    }
    laneCursor[stage] = y;
    void w;
  };
  stackLane('source', sourceNodes || []);
  stackLane('collect', taskNodes.filter((t) => t.stage === 'collect'));
  stackLane('process', taskNodes.filter((t) => t.stage === 'process'));

  // -- 区域/模块：条带之下，区域居中列、模块右列 ---------------------------
  // 业务线分组（2026-10-24）：组切换处插额外间距；落位后按组算标题带。
  let cursorY = stripBottom + 26;
  let lastGroupKey = null;
  for (const region of regionNodes) {
    const gk = region.group || '__none__';
    if (lastGroupKey !== null && gk !== lastGroupKey) cursorY += 22;
    lastGroupKey = gk;
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

  // 组标题带（按落位后的节点外接矩形；跟随用户拖动后的实际位置）
  const byGroup = new Map();
  for (const region of regionNodes) {
    const gk = region.group || '__none__';
    if (!byGroup.has(gk)) byGroup.set(gk, []);
    byGroup.get(gk).push(region);
  }
  const groupBands = [];
  for (const [gk, nodes] of byGroup) {
    const group = BUSINESS_GROUPS.find((g) => g.key === gk);
    const top = Math.min(...nodes.map((n) => n.y));
    const bottom = Math.max(
      ...nodes.map((n) => n.y + regionH(n, expandedRegions)));
    groupBands.push({
      key: gk,
      label: group ? group.label : '未分组',
      muted: !!(group && group.muted),
      x: REGION_X - 60,
      y: top - 12,
      h: bottom - top + 24,
    });
  }
  return { poolNode, groupBands };
}

// -- 连线几何 -------------------------------------------------------------------

/** 节点矩形表（连线端点取位用）：聚合=圆（边长 AGG_SIZE），区域高按展开态。
 * extras 可带池/全局节点（日历→提醒/催办的消费连线要锚到日历节点）。 */
export function nodeRects(regionNodes, taskNodes, expandedRegions, expandedModules,
                          { poolNode = null, globalNodes = [], sourceNodes = [] } = {}) {
  const map = {};
  for (const n of regionNodes) {
    map[n.id] = n.isAggregate
      ? { ...n, w: AGG_SIZE, h: AGG_SIZE }
      : { ...n, w: REGION_W, h: regionH(n, expandedRegions) };
  }
  for (const n of taskNodes) {
    map[n.id] = { ...n, w: TASK_W, h: moduleH(n, expandedModules) };
  }
  for (const n of globalNodes) {
    map[n.id] = { ...n, w: GLOBAL_W, h: GLOBAL_H };
  }
  for (const n of sourceNodes) {
    map[n.id] = { ...n, w: LANE_W.source, h: 56 };
  }
  if (poolNode) map.pool = { ...poolNode, w: POOL_W, h: GLOBAL_H };
  return map;
}

// -- 多方向拉线（2026-10-17 F5）-------------------------------------------------
// 端口方位 → 边法线单位向量（控制点沿法线延伸，连线从就近边自然进出）。
const SIDE_NORMAL = {
  left: [-1, 0],
  right: [1, 0],
  top: [0, -1],
  bottom: [0, 1],
};

/** 端口在节点矩形上的锚点（边中点）。 */
export function portAnchor(rect, side) {
  switch (side) {
    case 'left': return { x: rect.x, y: rect.y + rect.h / 2 };
    case 'right': return { x: rect.x + rect.w, y: rect.y + rect.h / 2 };
    case 'top': return { x: rect.x + rect.w / 2, y: rect.y };
    case 'bottom': return { x: rect.x + rect.w / 2, y: rect.y + rect.h };
    default: return { x: rect.x + rect.w, y: rect.y + rect.h / 2 };
  }
}

/**
 * 端口方位自动选择：按两矩形中心相对方位取对边——水平占优取 left/right 对，
 * 垂直占优取 top/bottom 对（默认三栏布局下结果=旧的「右出左入」）。
 */
export function pickSides(from, to) {
  const dx = (to.x + to.w / 2) - (from.x + from.w / 2);
  const dy = (to.y + to.h / 2) - (from.y + from.h / 2);
  if (Math.abs(dx) >= Math.abs(dy)) {
    return dx >= 0 ? ['right', 'left'] : ['left', 'right'];
  }
  return dy >= 0 ? ['bottom', 'top'] : ['top', 'bottom'];
}

/**
 * 统一边路径生成器（方向自适应贝塞尔）：控制点沿各端点所在边的法线方向
 * 延伸 max(slack, 端点距离/2)。全部连线的唯一收口点。
 */
export function edgePathSides(x1, y1, side1, x2, y2, side2, { slack = 60 } = {}) {
  const dist = Math.hypot(x2 - x1, y2 - y1);
  const c = Math.max(slack, dist / 2);
  const [nx1, ny1] = SIDE_NORMAL[side1] || SIDE_NORMAL.right;
  const [nx2, ny2] = SIDE_NORMAL[side2] || SIDE_NORMAL.left;
  return `M ${x1} ${y1} C ${x1 + nx1 * c} ${y1 + ny1 * c}, ${x2 + nx2 * c} ${y2 + ny2 * c}, ${x2} ${y2}`;
}

/**
 * 拉线草稿路径：自由端无节点，按光标相对起点的方位动态选边
 * （拖向左就从左边出、拖向下就从下边出，不再绕圈）。
 */
export function draftPath(x1, y1, x2, y2, { slack = 60 } = {}) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  let side1;
  let side2;
  if (Math.abs(dx) >= Math.abs(dy)) {
    side1 = dx >= 0 ? 'right' : 'left';
    side2 = dx >= 0 ? 'left' : 'right';
  } else {
    side1 = dy >= 0 ? 'bottom' : 'top';
    side2 = dy >= 0 ? 'top' : 'bottom';
  }
  return edgePathSides(x1, y1, side1, x2, y2, side2, { slack });
}

/** 区域→模块/汇聚边路径：按实际摆位自动选边（自由拖动后不变形）。 */
export function buildEdgePaths(edges, rects) {
  return edges.map((edge) => {
    const from = rects[edge.from];
    const to = rects[edge.to];
    if (!from || !to) return { ...edge, d: '' };
    const [s1, s2] = pickSides(from, to);
    const a = portAnchor(from, s1);
    const b = portAnchor(to, s2);
    return { ...edge, d: edgePathSides(a.x, a.y, s1, b.x, b.y, s2) };
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
      // F5：依赖线也按实际摆位自动选边（上下堆叠走垂直贝塞尔）
      const [s1, s2] = pickSides(from, to);
      const a = portAnchor(from, s1);
      const b = portAnchor(to, s2);
      out.push({
        id: key,
        fromLabel: fromModule.label,
        toLabel: n.label,
        fromModule,
        toModule: n,
        sourceIds: fromModule.services.map((s) => s.service_id),
        d: edgePathSides(a.x, a.y, s1, b.x, b.y, s2),
      });
    }
  }
  return out;
}

/** 画布滚动面尺寸：节点外接矩形 + 60 余量，下限 1300×480。 */
export function canvasSize(rects, globalNodes) {
  let maxX = 1900;   // 五泳道下限
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
