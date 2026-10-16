<script setup>
/**
 * 闭环画布 v3（2026-10-16，P1）：ComfyUI 式画布即应用——画布逐步吃掉
 * ops-web 各页（用户裁决「闭环画布 = ops-web」）。
 *
 * ComfyUI 语义落地：
 *   · 连线即关系写：未归类池成员节点的输出端口拉线 → 区域节点输入端口
 *     ＝ 归属变更（assignMember，confirm 后落库）；
 *   · 节点即控件：任务节点内嵌 启用开关 / 运行一次（写端点复用，零新增）；
 *   · 点节点开抽屉：任务抽屉＝改播报时间 + 执行流水入口（P1c）。
 *
 * 画布交互：滚轮缩放（光标锚点）、拖空白平移、拖节点头移动
 * （坐标存 localStorage v3）。名册/收件人/日历/授权分期吸收（P2-P4）。
 */
import { computed, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import {
  getOverview,
  getPipelines,
  getRoster,
  runPipelineOnce,
  togglePipeline,
} from '../api.js';
import { hasCapability, isAdmin, me, setFlash, flash } from '../store/me.js';
// F1：建图/布局纯函数（flow/graph.js、flow/layout.js）；
// F2：抽屉组件化（components/flow/drawers/ + flow/useDrawer.js）；
// F3（2026-10-17）：节点组件化（components/flow/）+ 交互 composable
// （useViewport/useNodePositions/useMemberWire/useDepWire）。
// 本文件=壳：图状态、装载编排、展开态、布局与连线的 computed 接线。
import { buildGraph as buildGraphModel } from '../flow/graph.js';
import { globalNodeKind, useDrawer } from '../flow/useDrawer.js';
import { useViewport } from '../flow/useViewport.js';
import { useNodePositions } from '../flow/useNodePositions.js';
import { useMemberWire } from '../flow/useMemberWire.js';
import { useDepWire } from '../flow/useDepWire.js';
import DrawerHost from '../components/flow/drawers/DrawerHost.vue';
import PoolNode from '../components/flow/PoolNode.vue';
import RegionNode from '../components/flow/RegionNode.vue';
import ModuleNode from '../components/flow/ModuleNode.vue';
import GlobalNodes from '../components/flow/GlobalNodes.vue';
import {
  TOP_Y,
  buildDepEdges,
  buildEdgePaths,
  canvasSize as canvasSizeFor,
  layoutNodes,
  moduleH as moduleNodeH,
  nodeRects,
} from '../flow/layout.js';

const route = useRoute();

function hasAnyCap(cap) {
  if (hasCapability(cap)) return true;
  const scoped = (me.value && me.value.scoped_capabilities
    && me.value.scoped_capabilities[cap]) || [];
  return scoped.length > 0;
}
const canRoster = computed(() => hasAnyCap('roster'));
const canPipelines = computed(() => hasAnyCap('pipelines'));

// -- 图状态 -----------------------------------------------------------------
const regionNodes = ref([]);
const taskNodes = ref([]);
const edges = ref([]);
const poolMembers = ref([]);
const month = ref('');
const loading = ref(true);
const loadError = ref('');
const busy = ref(false);       // 写接口在途：暂停拉线

const anchorRegion = computed(() =>
  typeof route.query.region === 'string' ? route.query.region : '',
);

// -- 视口（缩放/平移/节点拖拽；实现在 flow/useViewport.js）----------------------
const {
  zoom, panX, panY,
  viewportStyle, gridStyle,
  onWheel, startPan, resetView, startNodeDrag: viewportNodeDrag, focusOn,
} = useViewport();

// -- 节点坐标持久化（flow/useNodePositions.js，v3 + 旧版键迁移）------------------
const { nodePos, loadPos, resetAllPos, remember } = useNodePositions();

function onResetAllPos() {
  resetAllPos();
  layout();
}

/** 节点头拖拽：写接口在途时暂停（busy），松手记忆坐标。 */
function startNodeDrag(evt, node) {
  if (busy.value) return;
  viewportNodeDrag(evt, node, {
    onDrop: (n) => remember(n.id, n.x, n.y),
  });
}

//: 成员行展开状态（区域节点默认收起成员列表，保持画布清爽）
const expandedRegions = ref(new Set());

function toggleRegionMembers(node) {
  const next = new Set(expandedRegions.value);
  if (next.has(node.id)) next.delete(node.id);
  else next.add(node.id);
  expandedRegions.value = next;
  layout();   // 高度变化影响连线端点与层距
}

//: 写操作后的静默全量刷新（拉线/抽屉共用；load 声明在下方，函数提升兜底）
const reloadSilent = () => load({ silent: true });

const poolNode = ref({ id: 'pool', x: 40, y: TOP_Y });

// -- 拉线（实现在 flow/useMemberWire.js / useDepWire.js）-------------------------
const canWire = computed(() => canPipelines.value || canRoster.value);
const { wireDraft, wireDraftPath, startWire } = useMemberWire({
  busy, panX, panY, zoom, regionNodes, canWire, reload: reloadSilent,
});
const { depDraft, depDraftPath, startDepWire, onDepEdgeClick } = useDepWire({
  busy, panX, panY, zoom, taskNodes,
  moduleHOf: (node) => moduleNodeH(node, expandedModules.value),
  canPipelines, reload: reloadSilent,
});

// -- 抽屉（F2：状态机 + 组件化，实现见 components/flow/drawers/）-----------------
const { drawer, openDrawer, closeDrawer } = useDrawer();

// -- 全局节点（P3：播报日历 / 操作记录 上画布）--------------------------------
const globalNodes = ref([
  { id: 'global:calendar', label: '播报日历', desc: '点日期改播报/休息', x: 40, y: 400 },
  { id: 'global:records', label: '操作记录', desc: '六源审计流水', x: 40, y: 480 },
  { id: 'global:grants', label: '授权', desc: '能力×区域授权', x: 40, y: 560, admin: true },
  { id: 'global:datafix', label: '数据修正', desc: '类型化修数（预览→落库）', x: 40, y: 640, cap: 'datafix' },
]);

//: 数据修正/授权抽屉实现 → components/flow/drawers/{DataFixDrawer,GrantsDrawer}.vue

//: 日历/记录抽屉实现 → components/flow/drawers/{CalendarDrawer,RecordsDrawer}.vue

function onGlobalNodeClick(node) {
  const kind = globalNodeKind(node.id);
  if (kind) openDrawer(kind);
}

const visibleGlobalNodes = computed(() =>
  globalNodes.value.filter((n) =>
    (!n.admin || isAdmin()) && (!n.cap || hasAnyCap(n.cap))));

//: 任务抽屉收件人段/播报时间编辑 → components/flow/drawers/TaskDrawer.vue

async function onToggleTask(service) {
  const target = !service.enabled;
  busy.value = true;
  try {
    await togglePipeline({ service_id: service.service_id, enabled: target });
    setFlash('ok', `「${service.service_label}」已${target ? '启用' : '停用'}`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `开关失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onRunOnce(service) {
  const ok = window.confirm(
    `立即运行一次「${service.service_label}」？\n（手动触发不受当日去重限制，会真实发播报/写页面）`,
  );
  if (!ok) return;
  busy.value = true;
  try {
    await runPipelineOnce({ service_id: service.service_id });
    setFlash('ok', `「${service.service_label}」已提交运行，流水见任务抽屉`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `运行失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

// -- 数据派生 -------------------------------------------------------------------
const lastRaw = ref(null);

//: 聚合节点/补集桶常量与建图纯函数在 flow/graph.js（AGGREGATE_REGIONS、
//: COMPLEMENT_REGION、regionBadge、taskBroken、familyOf、familyLabel、fmt）。

/** raw → 图模型（纯函数在 flow/graph.js）；此处只灌状态并重排。 */
function buildGraph(raw) {
  const graph = buildGraphModel(raw);
  poolMembers.value = graph.poolMembers;
  regionNodes.value = graph.regionNodes;
  taskNodes.value = graph.taskNodes;
  edges.value = graph.edges;
  layout();
}

const expandedModules = ref(new Set());
const expandAll = ref(false);

function toggleModule(node) {
  const next = new Set(expandedModules.value);
  if (next.has(node.id)) next.delete(node.id);
  else next.add(node.id);
  expandedModules.value = next;
  layout();   // 高度变化影响下游连线端点
}

function toggleExpandAll() {
  expandAll.value = !expandAll.value;
  expandedModules.value = expandAll.value
    ? new Set(taskNodes.value.map((n) => n.id))
    : new Set();
  layout();
}

/** 自动布局（纯函数在 flow/layout.js）；池节点对象整体替换。 */
function layout() {
  poolNode.value = layoutNodes({
    regionNodes: regionNodes.value,
    taskNodes: taskNodes.value,
    poolMembers: poolMembers.value,
    globalNodes: globalNodes.value,
    savedPos: nodePos.value,
    expandedRegions: expandedRegions.value,
    expandedModules: expandedModules.value,
  });
}

// -- 连线几何（纯函数在 flow/layout.js，此处注入当前图状态）---------------------
const nodeById = computed(() => nodeRects(
  regionNodes.value, taskNodes.value,
  expandedRegions.value, expandedModules.value,
));

const edgePaths = computed(() => buildEdgePaths(edges.value, nodeById.value));

/** 依赖线（模块→模块）：成员服务 depends_on 聚合到家族模块层。 */
const depEdges = computed(() => buildDepEdges(taskNodes.value, nodeById.value));

const canvasSize = computed(() => canvasSizeFor(nodeById.value, globalNodes.value));

// -- 锚点 ------------------------------------------------------------------------
async function applyAnchor() {
  if (!anchorRegion.value) return;
  const node = regionNodes.value.find((n) => n.key === anchorRegion.value);
  if (!node) return;
  await focusOn(node.x, node.y);   // 视口偏移 260/120 与旧实现一致
}

// -- 装载 ------------------------------------------------------------------------
async function load({ silent = false } = {}) {
  if (!silent) loading.value = true;
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
    if (!silent) loading.value = false;
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
          连线即关系：矩形=业务区域、圆形=聚合（线下整体=杭州+绍兴+其他，靛蓝线汇聚）。
          拉线：池成员→区域＝归口、模块→模块＝依赖；点节点开抽屉（名册/目标/时间/收件人/日历/记录/授权/修正）。
        </p>
      </div>
      <div class="head-actions">
        <span v-if="month" class="month">目标月份 {{ month }}</span>
        <button class="btn" @click="toggleExpandAll">
          {{ expandAll ? '收起全部模块' : '展开全部模块' }}
        </button>
        <button class="btn" @click="onResetAllPos">复位布局</button>
        <button class="btn" @click="resetView">复位视图</button>
        <button class="btn" :disabled="loading" @click="load()">刷新</button>
      </div>
    </header>

    <transition name="flash">
      <div v-if="flash" class="flash" :class="flash.type">{{ flash.text }}</div>
    </transition>

    <div v-if="loading" class="state">加载中…</div>
    <div v-else-if="loadError" class="state error">{{ loadError }}</div>

    <div v-else class="canvas-wrap">
      <div
        class="canvas"
        :style="gridStyle"
        @wheel.prevent="onWheel"
        @mousedown="startPan"
      >
        <div class="viewport" :style="viewportStyle">
          <svg class="edges" :width="canvasSize.w" :height="canvasSize.h">
            <path
              v-for="edge in edgePaths"
              :key="edge.id"
              :d="edge.d"
              class="edge"
              :class="{ broken: edge.broken, converge: edge.converge }"
            />
            <path v-if="wireDraft" :d="wireDraftPath" class="edge drafting" />
            <path v-if="depDraft" :d="depDraftPath" class="edge dep-drafting" />
            <path
              v-for="dep in depEdges"
              :key="dep.id"
              :d="dep.d"
              class="edge dep"
              :data-dep-id="dep.id"
              @mousedown.stop
              @click="onDepEdgeClick(dep)"
            ><title>依赖：{{ dep.fromLabel }} → {{ dep.toLabel }}（点击解除）</title></path>
          </svg>

          <!-- 未归类池节点（成员输出端口：拉线到区域＝归口） -->
          <PoolNode
            :node="poolNode"
            :members="poolMembers"
            :busy="busy"
            @drag="startNodeDrag"
            @wire-start="startWire"
          />

          <!-- 区域节点（聚合=圆形，业务=矩形） -->
          <RegionNode
            v-for="node in regionNodes"
            :key="node.id"
            :node="node"
            :expanded="expandedRegions.has(node.id)"
            :anchor-hit="anchorRegion === node.key"
            :busy="busy"
            @drag="startNodeDrag"
            @wire-start="startWire"
            @toggle-members="toggleRegionMembers"
            @open-drawer="(n) => openDrawer('region', n)"
          />

          <!-- 全局节点（播报日历 / 操作记录 / 授权 / 数据修正） -->
          <GlobalNodes
            :nodes="visibleGlobalNodes"
            @drag="startNodeDrag"
            @open="onGlobalNodeClick"
          />

          <!-- 家族模块节点（区域→模块 N:1；点头部展开成员） -->
          <ModuleNode
            v-for="node in taskNodes"
            :key="node.id"
            :node="node"
            :expanded="expandedModules.has(node.id)"
            :busy="busy"
            @drag="startNodeDrag"
            @toggle="toggleModule"
            @dep-wire-start="startDepWire"
            @toggle-task="onToggleTask"
            @run-once="onRunOnce"
            @open-drawer="(s) => openDrawer('task', { service: s })"
          />
        </div>
      </div>

      <div class="legend">
        <span class="lg"><i class="line ok" /> 正常驱动</span>
        <span class="lg"><i class="line broken" /> 断链（缺名册/缺目标/停用/失败）</span>
        <span class="lg"><i class="line converge" /> 汇聚（桶→聚合）</span>
        <span class="lg"><i class="line drafting" /> 拉线中＝归口</span>
        <span class="lg">圆形＝聚合节点 · 滚轮缩放 · 拖空白平移 · 拖节点头移动</span>
      </div>
    </div>

    <!-- 节点抽屉（F2：宿主 + 按 kind 动态分发到 drawers/ 组件） -->
    <DrawerHost
      :drawer="drawer"
      :busy="busy"
      :month="month"
      :roster="lastRaw && lastRaw.roster"
      :region-nodes="regionNodes"
      :reload="reloadSilent"
      @close="closeDrawer"
      @update:busy="busy = $event"
    />
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
.btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.btn.mini {
  padding: 3px 8px;
  font-size: 12px;
}
.btn.ghost {
  border-color: transparent;
  color: #4b5563;
}
.btn.primary {
  background: #2563eb;
  border-color: #2563eb;
  color: #fff;
  margin-left: 8px;
}
.flash {
  margin: 10px 0 0;
  padding: 9px 12px;
  border-radius: 6px;
  font-size: 13px;
}
.flash.ok {
  background: #ecfdf5;
  border: 1px solid #a7f3d0;
  color: #047857;
}
.flash.err {
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #b91c1c;
}
.flash-enter-active,
.flash-leave-active {
  transition: opacity 0.2s ease;
}
.flash-enter-from,
.flash-leave-to {
  opacity: 0;
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
.edge.converge {
  stroke: #6366f1;
  stroke-width: 2.5;
}

/* 节点样式已随 F3 组件化迁往 components/flow/{PoolNode,RegionNode,ModuleNode,GlobalNodes}.vue */
.edge.drafting {
  stroke: #2563eb;
  stroke-dasharray: 4 4;
}
.edge.dep {
  stroke: #8b5cf6;
  stroke-width: 2;
  stroke-dasharray: 2 3;
  pointer-events: stroke;
  cursor: pointer;
}
.edge.dep:hover {
  stroke: #7c3aed;
  stroke-width: 3;
}
.edge.dep-drafting {
  stroke: #8b5cf6;
  stroke-dasharray: 4 4;
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
.line.drafting {
  border-top: 2px dashed #2563eb;
}
.line.converge {
  border-top: 2.5px solid #6366f1;
}

/* 抽屉样式在 components/flow/drawers/drawer.css（.flow-drawer 命名空间） */
</style>
