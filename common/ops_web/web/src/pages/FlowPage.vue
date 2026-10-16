<script setup>
/**
 * 闭环画布 v2（2026-10-16）：ComfyUI 式纯读拓扑画布。
 *
 * 语义裁决：画布只表达「谁驱动谁」的拓扑——写操作各归其位
 * （归属=组织编排页、名册=名册页、收件人=定时任务页），画布零写接口。
 *
 * 节点两类：
 *   · 区域节点（左）：名册/目标/成员/Σ万元 四要素 + 闭环状态徽标
 *   · 任务节点（右）：该区域下游定时任务（schedule/启用态/最近运行）
 * 连线：区域输出端口 →（驱动）→ 任务输入端口，贝塞尔曲线；
 * 断链（缺名册/缺目标/任务停用/最近失败）= 琥珀虚线。
 *
 * 画布交互：滚轮缩放（光标为中心）、拖空白平移、拖节点头移动节点
 * （坐标存 localStorage，v2 不落库）、?region= 锚点定位。
 * 读面并行拉既有 GET（overview / roster / pipelines），零新增端点。
 */
import { computed, nextTick, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { getOverview, getPipelines, getRoster } from '../api.js';
import { hasCapability, me } from '../store/me.js';

const route = useRoute();

// -- 能力（含区域级）------------------------------------------------------
function hasAnyCap(cap) {
  if (hasCapability(cap)) return true;
  const scoped = (me.value && me.value.scoped_capabilities
    && me.value.scoped_capabilities[cap]) || [];
  return scoped.length > 0;
}
const canRoster = computed(() => hasAnyCap('roster'));
const canPipelines = computed(() => hasAnyCap('pipelines'));

// -- 状态 -----------------------------------------------------------------
const regionNodes = ref([]);   // 区域节点（含「平台/无群」伪区域）
const taskNodes = ref([]);     // 任务节点
const edges = ref([]);         // 连线（region → task）
const poolMembers = ref([]);   // 未归类池（只读展示）
const month = ref('');
const loading = ref(true);
const loadError = ref('');

const anchorRegion = computed(() =>
  typeof route.query.region === 'string' ? route.query.region : '',
);

// -- 画布视口（缩放/平移）-------------------------------------------------
const zoom = ref(1);
const panX = ref(0);
const panY = ref(0);
const ZOOM_MIN = 0.3;
const ZOOM_MAX = 2;

const viewportStyle = computed(() => ({
  transform: `translate(${panX.value}px, ${panY.value}px) scale(${zoom.value})`,
}));
const gridStyle = computed(() => ({
  backgroundSize: `${22 * zoom.value}px ${22 * zoom.value}px`,
  backgroundPosition: `${panX.value}px ${panY.value}px`,
}));

function onWheel(evt) {
  const old = zoom.value;
  const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN,
    old * (evt.deltaY < 0 ? 1.12 : 1 / 1.12)));
  if (next === old) return;
  // 以光标为锚缩放：保持光标下的画布点不动
  const rect = evt.currentTarget.getBoundingClientRect();
  const cx = evt.clientX - rect.left;
  const cy = evt.clientY - rect.top;
  panX.value = cx - ((cx - panX.value) / old) * next;
  panY.value = cy - ((cy - panY.value) / old) * next;
  zoom.value = next;
}

function startPan(evt) {
  if (evt.button !== 0) return;
  const startX = evt.clientX;
  const startY = evt.clientY;
  const originX = panX.value;
  const originY = panY.value;
  function onMove(moveEvt) {
    panX.value = originX + moveEvt.clientX - startX;
    panY.value = originY + moveEvt.clientY - startY;
  }
  function onUp() {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
  }
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}

function resetView() {
  zoom.value = 1;
  panX.value = 0;
  panY.value = 0;
}

// -- 节点坐标（localStorage 持久化）---------------------------------------
const POS_KEY = 'ops-web.flow.node-pos.v2';
const nodePos = ref({});

function loadPos() {
  try {
    nodePos.value = JSON.parse(localStorage.getItem(POS_KEY) || '{}') || {};
  } catch (err) {
    nodePos.value = {};
  }
}
function savePos() {
  try {
    localStorage.setItem(POS_KEY, JSON.stringify(nodePos.value));
  } catch (err) {
    /* 隐私模式/配额满：坐标持久化失败不影响展示 */
  }
}
function resetAllPos() {
  nodePos.value = {};
  savePos();
  layout();
}

// -- 布局常量 --------------------------------------------------------------
const REGION_W = 300;
const REGION_H = 168;
const TASK_W = 330;
const TASK_H = 96;
const TASK_GAP = 14;
const REGION_X = 40;
const TASK_X = 560;
const TOP_Y = 40;

// -- 节点拖拽（头部拖动＝改坐标）-------------------------------------------
function startNodeDrag(evt, node) {
  if (evt.button !== 0) return;
  evt.stopPropagation();
  const startX = evt.clientX;
  const startY = evt.clientY;
  const originX = node.x;
  const originY = node.y;
  function onMove(moveEvt) {
    node.x = Math.max(0, originX + (moveEvt.clientX - startX) / zoom.value);
    node.y = Math.max(0, originY + (moveEvt.clientY - startY) / zoom.value);
  }
  function onUp() {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    nodePos.value[node.id] = { x: Math.round(node.x), y: Math.round(node.y) };
    savePos();
  }
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}

// -- 数据装载 --------------------------------------------------------------
const lastRaw = ref(null);

function regionBadge(rosterCount, targetCount, taskCount) {
  if (rosterCount === 0) return { text: '缺名册', cls: 'warn' };
  if (targetCount === 0) return { text: '缺目标', cls: 'warn' };
  if (!taskCount) return { text: '待下游', cls: 'warn' };
  return { text: '已闭环', cls: 'ok' };
}

function fmt(value) {
  const num = Number(value) || 0;
  return Number.isInteger(num) ? String(num) : num.toFixed(1);
}

/** 任务是否处于断链态（停用或最近运行失败）。 */
function taskBroken(service) {
  if (!service.enabled) return true;
  const latest = service.latest;
  return !!(latest && latest.status === 'failed');
}

/** 由原始读面派生节点与连线。 */
function buildGraph(raw) {
  const { overview, roster, groups } = raw;
  const rosterRows = (roster && roster.roster) || [];
  const targets = (roster && roster.targets) || {};
  const regions = (overview && overview.regions) || [];

  poolMembers.value = ((overview && overview.pool) || []).slice(0, 50);

  const servicesByRegion = {};
  for (const group of groups || []) {
    servicesByRegion[group.region === null ? '__platform__' : group.region] =
      group.services || [];
  }

  const nextRegions = [];
  const nextTasks = [];
  const nextEdges = [];
  for (const region of regions) {
    const key = region.key;
    const memberCount = (region.depts || [])
      .reduce((sum, dept) => sum + (dept.members || []).length, 0);
    const rosterCount = rosterRows
      .filter((row) => row.scope === key && Number(row.enabled) === 1).length;
    const targetMap = targets[key] || {};
    const targetCount = Object.keys(targetMap).length;
    const targetSum = Object.values(targetMap)
      .reduce((sum, value) => sum + (Number(value) || 0), 0);
    const services = servicesByRegion[key] || [];
    const badge = regionBadge(rosterCount, targetCount, services.length);
    nextRegions.push({
      id: `region:${key}`,
      key,
      label: region.label || key,
      badge,
      rosterCount,
      targetCount,
      targetSum,
      memberCount,
      taskCount: services.length,
      x: REGION_X,
      y: TOP_Y,
    });
    services.forEach((service, index) => {
      const taskId = `task:${service.service_id}`;
      nextTasks.push({
        id: taskId,
        regionKey: key,
        index,
        service,
        broken: taskBroken(service),
        x: TASK_X,
        y: TOP_Y,
      });
      nextEdges.push({
        id: `e:${key}:${service.service_id}`,
        from: `region:${key}`,
        to: taskId,
        broken: badge.cls !== 'ok' || taskBroken(service),
      });
    });
  }
  // 平台/无群任务（region=None）挂到伪区域节点
  const platform = servicesByRegion.__platform__ || [];
  if (platform.length) {
    nextRegions.push({
      id: 'region:__platform__',
      key: '__platform__',
      label: '平台 / 无群',
      badge: { text: '平台线', cls: 'plain' },
      rosterCount: 0,
      targetCount: 0,
      targetSum: 0,
      memberCount: 0,
      taskCount: platform.length,
      x: REGION_X,
      y: TOP_Y,
    });
    platform.forEach((service, index) => {
      const taskId = `task:${service.service_id}`;
      nextTasks.push({
        id: taskId,
        regionKey: '__platform__',
        index,
        service,
        broken: taskBroken(service),
        x: TASK_X,
        y: TOP_Y,
      });
      nextEdges.push({
        id: `e:__platform__:${service.service_id}`,
        from: 'region:__platform__',
        to: taskId,
        broken: taskBroken(service),
      });
    });
  }
  regionNodes.value = nextRegions;
  taskNodes.value = nextTasks;
  edges.value = nextEdges;
  layout();
}

/** 默认布局：任务按所属区域纵向成摞，区域节点垂直居中对齐其任务摞。 */
function layout() {
  const saved = nodePos.value;
  const taskYById = {};
  let cursorY = TOP_Y;
  for (const region of regionNodes.value) {
    const stack = taskNodes.value.filter((t) => t.regionKey === region.key);
    const stackTop = cursorY;
    stack.forEach((task) => {
      taskYById[task.id] = cursorY;
      cursorY += TASK_H + TASK_GAP;
    });
    const stackH = stack.length * (TASK_H + TASK_GAP) - TASK_GAP;
    const regionY = stack.length
      ? stackTop + Math.max(0, (stackH - REGION_H) / 2)
      : cursorY;
    cursorY = Math.max(cursorY, stack.length ? stackTop : cursorY)
      + (stack.length ? 0 : REGION_H) + 26;
    const savedPos = saved[region.id];
    region.x = savedPos ? savedPos.x : REGION_X;
    region.y = savedPos ? savedPos.y : Math.round(regionY);
  }
  for (const task of taskNodes.value) {
    const savedPos = saved[task.id];
    task.x = savedPos ? savedPos.x : TASK_X;
    task.y = savedPos ? savedPos.y : taskYById[task.id];
  }
}

// -- 连线几何（贝塞尔）-----------------------------------------------------
const nodeById = computed(() => {
  const map = {};
  for (const n of regionNodes.value) map[n.id] = { ...n, w: REGION_W, h: REGION_H };
  for (const n of taskNodes.value) map[n.id] = { ...n, w: TASK_W, h: TASK_H };
  return map;
});

const edgePaths = computed(() => edges.value.map((edge) => {
  const from = nodeById.value[edge.from];
  const to = nodeById.value[edge.to];
  if (!from || !to) return { ...edge, d: '' };
  const x1 = from.x + from.w;
  const y1 = from.y + from.h / 2;
  const x2 = to.x;
  const y2 = to.y + 34;
  const dx = Math.max(60, (x2 - x1) / 2);
  return {
    ...edge,
    d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`,
  };
}));

const canvasSize = computed(() => {
  let maxX = 960;
  let maxY = 480;
  for (const n of Object.values(nodeById.value)) {
    maxX = Math.max(maxX, n.x + n.w + 60);
    maxY = Math.max(maxY, n.y + n.h + 60);
  }
  return { w: maxX, h: maxY };
});

// -- 锚点 -------------------------------------------------------------------
async function applyAnchor() {
  if (!anchorRegion.value) return;
  await nextTick();
  const node = regionNodes.value.find((n) => n.key === anchorRegion.value);
  if (!node) return;
  // 平移视口让目标节点大致居中（缩放保持）
  panX.value = 260 - node.x * zoom.value;
  panY.value = 120 - node.y * zoom.value;
}

// -- 装载 -------------------------------------------------------------------
async function load() {
  loading.value = true;
  loadError.value = '';
  try {
    const [overview, roster, pipelines] = await Promise.all([
      getOverview(),
      canRoster.value ? getRoster() : Promise.resolve(null),
      canPipelines.value ? getPipelines() : Promise.resolve(null),
    ]);
    month.value = (roster && roster.month) || '';
    lastRaw.value = {
      overview,
      roster,
      groups: (pipelines && pipelines.groups) || [],
    };
    buildGraph(lastRaw.value);
  } catch (err) {
    loadError.value = err.message;
  } finally {
    loading.value = false;
  }
  await applyAnchor();
}

loadPos();
load();
watch(() => route.query.region, () => applyAnchor());
</script>

<template>
  <div class="page">
    <header class="head">
      <div class="head-main">
        <h1>闭环画布</h1>
        <p class="note">
          组织 → 名册 → 目标 → 定时任务 → 记录 的拓扑总览：连线即驱动关系，
          琥珀虚线＝断链（缺名册/缺目标/任务停用/最近失败）。纯读视图——
          改归属去「组织编排」、改名册去「填报名册」、改收件人去「定时任务」。
        </p>
      </div>
      <div class="head-actions">
        <span v-if="month" class="month">目标月份 {{ month }}</span>
        <button class="btn" @click="resetAllPos">复位布局</button>
        <button class="btn" @click="resetView">复位视图</button>
        <button class="btn" :disabled="loading" @click="load()">刷新</button>
      </div>
    </header>

    <div v-if="loading" class="state">加载中…</div>
    <div v-else-if="loadError" class="state error">{{ loadError }}</div>

    <div v-else class="canvas-wrap">
      <!-- 未归类池（只读角条） -->
      <aside class="pool-bar" v-if="poolMembers.length">
        <b>未归类池 {{ poolMembers.length }}</b>
        <span
          v-for="member in poolMembers"
          :key="member.user_id"
          class="pool-chip"
        >{{ member.name || member.user_id }}</span>
        <router-link class="pool-go" to="/org">去归口 →</router-link>
      </aside>

      <div
        class="canvas"
        :style="gridStyle"
        @wheel.prevent="onWheel"
        @mousedown="startPan"
      >
        <div class="viewport" :style="viewportStyle">
          <svg
            class="edges"
            :width="canvasSize.w"
            :height="canvasSize.h"
          >
            <path
              v-for="edge in edgePaths"
              :key="edge.id"
              :d="edge.d"
              class="edge"
              :class="{ broken: edge.broken }"
            />
          </svg>

          <!-- 区域节点 -->
          <article
            v-for="node in regionNodes"
            :key="node.id"
            :id="`flow-${node.key}`"
            class="node region-node"
            :class="{ 'anchor-hit': anchorRegion === node.key }"
            :style="{ left: node.x + 'px', top: node.y + 'px', width: REGION_W + 'px' }"
          >
            <div class="node-head" title="拖动移动节点" @mousedown="startNodeDrag($event, node)">
              <h3>{{ node.label }}</h3>
              <span class="badge" :class="node.badge.cls">{{ node.badge.text }}</span>
            </div>
            <div class="metrics" v-if="node.key !== '__platform__'">
              <router-link class="metric" :to="{ path: '/roster', query: { region: node.key } }">
                <b>{{ node.rosterCount }}</b><span>名册</span>
              </router-link>
              <router-link class="metric" :to="{ path: '/roster', query: { region: node.key } }">
                <b>{{ node.targetCount }}</b><span>目标人</span>
              </router-link>
              <span class="metric"><b>{{ fmt(node.targetSum) }}</b><span>Σ 万元</span></span>
              <router-link class="metric" :to="{ path: '/org', query: { region: node.key } }">
                <b>{{ node.memberCount }}</b><span>成员</span>
              </router-link>
            </div>
            <div class="node-foot">
              <span class="task-count">下游任务 {{ node.taskCount }}</span>
            </div>
            <span class="port out" title="驱动下游任务" />
          </article>

          <!-- 任务节点 -->
          <article
            v-for="node in taskNodes"
            :key="node.id"
            class="node task-node"
            :class="{ broken: node.broken, disabled: !node.service.enabled }"
            :style="{ left: node.x + 'px', top: node.y + 'px', width: TASK_W + 'px' }"
          >
            <span class="port in" />
            <div class="node-head" title="拖动移动节点" @mousedown="startNodeDrag($event, node)">
              <h4>{{ node.service.service_label }}</h4>
              <span class="cat">{{ node.service.category_label }}</span>
            </div>
            <div class="task-meta">
              <span>{{ node.service.schedule_zh || node.service.schedule || '—' }}</span>
              <span :class="node.service.enabled ? 'on' : 'off'">
                {{ node.service.enabled ? '启用' : '停用' }}
              </span>
              <span v-if="node.service.latest" class="latest">
                最近 {{ node.service.latest.status_label }}
              </span>
            </div>
          </article>
        </div>
      </div>

      <div class="legend">
        <span class="lg"><i class="line ok" /> 正常驱动</span>
        <span class="lg"><i class="line broken" /> 断链（缺名册/缺目标/停用/失败）</span>
        <span class="lg">滚轮缩放 · 拖空白平移 · 拖节点头移动</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  height: calc(100vh - 52px);
  display: flex;
  flex-direction: column;
  padding: 16px 20px;
  color: #1f2933;
  font-family: system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  font-size: 14px;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  flex-wrap: wrap;
}
.head-main h1 {
  margin: 0 0 4px;
  font-size: 20px;
}
.note {
  margin: 0;
  max-width: 860px;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.5;
}
.head-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.month {
  color: #6b7280;
  font-size: 13px;
}
.btn {
  padding: 7px 12px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  background: #fff;
  color: #1f2933;
  font-size: 13px;
  cursor: pointer;
}
.btn:hover {
  background: #f9fafb;
}
.state {
  padding: 48px;
  text-align: center;
  color: #6b7280;
}
.state.error {
  color: #b91c1c;
}

.canvas-wrap {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  margin-top: 12px;
}
.pool-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 8px 12px;
  margin-bottom: 10px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
  font-size: 12px;
  color: #4b5563;
}
.pool-chip {
  padding: 2px 8px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #f9fafb;
}
.pool-go {
  margin-left: auto;
  color: #2563eb;
  text-decoration: none;
}

.canvas {
  flex: 1;
  min-height: 0;
  position: relative;
  overflow: hidden;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background:
    radial-gradient(#dfe3e8 1px, transparent 1px) 0 0 / 22px 22px,
    #f7f8fa;
  cursor: grab;
}
.canvas:active {
  cursor: grabbing;
}
.viewport {
  position: absolute;
  left: 0;
  top: 0;
  transform-origin: 0 0;
}
.edges {
  position: absolute;
  left: 0;
  top: 0;
  pointer-events: none;
}
.edge {
  fill: none;
  stroke: #10b981;
  stroke-width: 2;
}
.edge.broken {
  stroke: #f59e0b;
  stroke-dasharray: 6 4;
}

.node {
  position: absolute;
  border: 1px solid #e2e5ea;
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 1px 3px rgba(16, 24, 40, 0.08);
}
.node.anchor-hit {
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.18);
}
.node-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px 4px;
  cursor: move;
  user-select: none;
}
.node-head h3 {
  margin: 0;
  font-size: 15px;
}
.node-head h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
}
.badge {
  padding: 1px 7px;
  border-radius: 10px;
  font-size: 11px;
}
.badge.ok {
  background: #ecfdf5;
  color: #047857;
}
.badge.warn {
  background: #fffbeb;
  color: #b45309;
}
.badge.plain {
  background: #f3f4f6;
  color: #6b7280;
}

.metrics {
  display: flex;
  gap: 6px;
  padding: 4px 10px;
}
.metric {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 52px;
  padding: 3px 6px;
  border: 1px solid #eef0f3;
  border-radius: 6px;
  background: #f9fafb;
  color: #1f2933;
  text-decoration: none;
  font-size: 11px;
}
.metric b {
  font-size: 14px;
}
.metric span {
  color: #6b7280;
}
a.metric:hover {
  border-color: #93c5fd;
  background: #eff6ff;
}
.node-foot {
  padding: 2px 10px 8px;
  font-size: 11px;
  color: #9ca3af;
}

.task-node {
  padding-bottom: 6px;
}
.task-node.disabled {
  opacity: 0.62;
}
.task-node.broken {
  border-color: #fbbf24;
}
.cat {
  padding: 1px 6px;
  border-radius: 8px;
  background: #eef2ff;
  color: #4338ca;
  font-size: 11px;
}
.task-meta {
  display: flex;
  gap: 10px;
  padding: 0 10px;
  font-size: 11px;
  color: #6b7280;
}
.task-meta .on {
  color: #047857;
}
.task-meta .off {
  color: #b91c1c;
}
.task-meta .latest {
  color: #9ca3af;
}

/* 端口 */
.port {
  position: absolute;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 2px solid #fff;
  box-shadow: 0 0 0 1px #cbd2da;
}
.port.out {
  right: -6px;
  top: 50%;
  margin-top: -5px;
  background: #10b981;
}
.port.in {
  left: -6px;
  top: 29px;
  background: #64748b;
}

.legend {
  display: flex;
  gap: 18px;
  padding: 8px 4px 0;
  font-size: 12px;
  color: #6b7280;
}
.lg {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.line {
  display: inline-block;
  width: 22px;
  border-top: 2px solid #10b981;
}
.line.broken {
  border-top: 2px dashed #f59e0b;
}
</style>
