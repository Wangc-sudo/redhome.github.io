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
import { buildGraph as buildGraphModel, STAGE_LABEL } from '../flow/graph.js';
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
  LANE_X,
  TOP_Y,
  buildDepEdges,
  buildEdgePaths,
  canvasSize as canvasSizeFor,
  layoutNodes,
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

//: 写操作后的静默刷新（F4：调用方带 parts 按域局部拉取；load 声明在下方，
//: 函数提升兜底）
const reloadSilent = (parts) => load({ silent: true, parts });

const poolNode = ref({ id: 'pool', x: 40, y: TOP_Y });

// -- 拉线（实现在 flow/useMemberWire.js / useDepWire.js）-------------------------
const canWire = computed(() => canPipelines.value || canRoster.value);
const { wireDraft, wireDraftPath, startWire } = useMemberWire({
  busy, panX, panY, zoom, regionNodes, canWire, reload: reloadSilent,
});
const { depDraft, depDraftPath, startDepWire, onDepEdgeClick } = useDepWire({
  busy, panX, panY, zoom, taskNodes,
  canPipelines, reload: reloadSilent,
});

// -- 抽屉（F2：状态机 + 组件化，实现见 components/flow/drawers/）-----------------
const { drawer, openDrawer, closeDrawer } = useDrawer();

// -- 全局节点（P3：播报日历 / 操作记录；P4 授权/修正；治理 P1 治理报告；
// 2026-10-19 播报试投——测试区改制为通用功能模块，用户裁决）--------------
const globalNodes = ref([
  { id: 'global:calendar', label: '播报日历', desc: '点日期改播报/休息', x: 40, y: 400 },
  { id: 'global:records', label: '操作记录', desc: '六源审计流水', x: 40, y: 480 },
  { id: 'global:governance', label: '治理报告', desc: '停用/僵尸/依赖线体检', x: 40, y: 560, cap: 'pipelines' },
  { id: 'global:relay', label: '播报试投', desc: '区域播报改投功能验证群', x: 40, y: 640, cap: 'org' },
  { id: 'global:grants', label: '授权', desc: '能力×区域授权', x: 40, y: 720, admin: true },
  { id: 'global:datafix', label: '数据修正', desc: '类型化修数（预览→落库）', x: 40, y: 800, cap: 'datafix' },
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
  let reason = '';
  let reviewAfter = '';
  if (!target) {
    // 治理门禁（2026-10-17）：停用必须带原因，可选复查日
    reason = (window.prompt(
      `停用「${service.service_label}」的原因？（必填，进审计）`,
      service.disabled_reason || '',
    ) || '').trim();
    if (!reason) {
      setFlash('err', '已取消：停用必须填原因');
      return;
    }
    reviewAfter = (window.prompt(
      '预计复查日（YYYY-MM-DD，可空）',
      service.review_after || '',
    ) || '').trim();
  }
  busy.value = true;
  try {
    await togglePipeline({
      service_id: service.service_id,
      enabled: target,
      reason,
      review_after: reviewAfter,
    });
    setFlash('ok', `「${service.service_label}」已${target ? '启用' : '停用'}`);
    await load({ silent: true, parts: ['pipelines'] });
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
    await load({ silent: true, parts: ['pipelines'] });
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
  sourceNodes.value = graph.sourceNodes || [];
  regionNodes.value = graph.regionNodes;
  taskNodes.value = graph.taskNodes;
  edges.value = graph.edges;
  // 默认折叠全部区域任务栈（2026-10-22 用户裁决 B：首开清爽），只首次初始化
  if (!tasksCollapseInit.value) {
    collapsedTaskRegions.value = new Set(taskNodes.value.map((t) => t.regionKey));
    tasksCollapseInit.value = true;
  }
  layout();
}

//: 流水线源节点（源→采集→加工泳道，2026-10-17 五层流水线改版）
const sourceNodes = ref([]);

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

//: 区域任务折叠（2026-10-22 用户裁决 B）：按区域折叠应用泳道的任务栈，
//: 组内连线（区域→任务汇聚边）随之收起，画布默认清爽；点区域节点「任务」展开细看。
const collapsedTaskRegions = ref(new Set());
const tasksCollapseInit = ref(false);

//: 参与布局/连线的任务 = 全部 − 已折叠区域的（折叠区任务不占位、不出线）。
const visibleTaskNodes = computed(() =>
  taskNodes.value.filter((t) => !collapsedTaskRegions.value.has(t.regionKey)));

const allTasksCollapsed = computed(() => {
  const keys = [...new Set(taskNodes.value.map((t) => t.regionKey))];
  return keys.length > 0 && keys.every((k) => collapsedTaskRegions.value.has(k));
});

function toggleRegionTasks(regionKey) {
  const next = new Set(collapsedTaskRegions.value);
  if (next.has(regionKey)) next.delete(regionKey);
  else next.add(regionKey);
  collapsedTaskRegions.value = next;
  layout();   // 折叠影响任务栈高度与连线端点
}

function toggleAllRegionTasks() {
  const keys = [...new Set(taskNodes.value.map((t) => t.regionKey))];
  const allCollapsed = keys.length > 0
    && keys.every((k) => collapsedTaskRegions.value.has(k));
  collapsedTaskRegions.value = allCollapsed ? new Set() : new Set(keys);
  layout();
}

/** 自动布局（纯函数在 flow/layout.js）；池节点对象整体替换。 */
function layout() {
  poolNode.value = layoutNodes({
    regionNodes: regionNodes.value,
    taskNodes: visibleTaskNodes.value,
    poolMembers: poolMembers.value,
    globalNodes: globalNodes.value,
    sourceNodes: sourceNodes.value,
    savedPos: nodePos.value,
    expandedRegions: expandedRegions.value,
    expandedModules: expandedModules.value,
  });
}

// -- 连线几何（纯函数在 flow/layout.js，此处注入当前图状态）---------------------
const nodeById = computed(() => nodeRects(
  regionNodes.value, visibleTaskNodes.value,
  expandedRegions.value, expandedModules.value,
  { poolNode: poolNode.value, globalNodes: globalNodes.value,
    sourceNodes: sourceNodes.value },
));

//: 五层流水线泳道标题（源→采集→加工→区域→应用）。
const LANES = Object.entries(STAGE_LABEL)
  .map(([key, label]) => ({ key, label, x: LANE_X[key] }));
const laneY = computed(() =>
  stripBand.value.y + stripBand.value.h + 6);

// 收线器（2026-10-17 用户裁决）：日历驱动消费线是画布最大噪音源
// （顶条扇出到每个提醒/催办模块），默认收起、头部开关按需展开。
const showConsumeEdges = ref(false);

//: 收线器（2026-10-22 用户裁决 A）：depends_on 依赖线（黄虚线，模块→模块）
//: 是画布第二大噪音源（跨泳道交叉缠绕），默认收起、头部开关按需展开。
const showDepEdges = ref(false);

const edgePaths = computed(() =>
  buildEdgePaths(edges.value, nodeById.value)
    .filter((e) => !e.consumes || showConsumeEdges.value));

/** 依赖线（模块→模块）：成员服务 depends_on 聚合到家族模块层。 */
const depEdges = computed(() => buildDepEdges(visibleTaskNodes.value, nodeById.value));

const canvasSize = computed(() => canvasSizeFor(nodeById.value, globalNodes.value));

/** 顶部功能条带的底板范围（池 + 全局节点外接矩形 + 内边距）。 */
const stripBand = computed(() => {
  let right = 0;
  let bottom = 0;
  const nodes = [poolNode.value, ...globalNodes.value];
  for (const n of nodes) {
    if (!n) continue;
    right = Math.max(right, (n.x || 0) + (n.id === 'pool' ? 220 : 220));
    bottom = Math.max(bottom, (n.y || 0) + 64);
  }
  // 池行高按成员数实际高度
  const poolH = 44 + poolMembers.value.length * 30 + 12;
  bottom = Math.max(bottom, poolNode.value.y + poolH);
  return { x: 16, y: poolNode.value.y - 16, w: right + 16, h: bottom - poolNode.value.y + 32 };
});

// -- 锚点 ------------------------------------------------------------------------
async function applyAnchor() {
  if (!anchorRegion.value) return;
  const node = regionNodes.value.find((n) => n.key === anchorRegion.value);
  if (!node) return;
  await focusOn(node.x, node.y);   // 视口偏移 260/120 与旧实现一致
}

// -- 装载 ------------------------------------------------------------------------
//: F4（2026-10-17）按域局部刷新：parts 子集时只重拉受影响域并并入 lastRaw
//: （未拉域沿用旧值），图重建管线不变（视觉语义零变化）；缺省三域全拉
//: （初次装载/手动刷新/未知影响面）。
async function load({ silent = false, parts = null } = {}) {
  if (!silent) loading.value = true;
  loadError.value = '';
  const want = parts || ['overview', 'roster', 'pipelines'];
  const prev = lastRaw.value || {};
  try {
    const [overview, roster, pipelines] = await Promise.all([
      want.includes('overview')
        ? getOverview()
        : Promise.resolve(prev.overview || null),
      want.includes('roster')
        ? (canRoster.value ? getRoster() : Promise.resolve(null))
        : Promise.resolve(prev.roster || null),
      want.includes('pipelines')
        ? (canPipelines.value ? getPipelines() : Promise.resolve(null))
        : Promise.resolve(prev.pipelines || null),
    ]);
    month.value = (roster && roster.month) || month.value;
    lastRaw.value = {
      overview,
      roster,
      groups: pipelines ? pipelines.groups : (prev.groups || []),
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
        <button
          class="btn"
          :class="{ 'btn-active': showConsumeEdges }"
          @click="showConsumeEdges = !showConsumeEdges"
        >
          {{ showConsumeEdges ? '收起日历驱动线' : '日历驱动线' }}
        </button>
        <button
          class="btn"
          :class="{ 'btn-active': showDepEdges }"
          @click="showDepEdges = !showDepEdges"
        >
          {{ showDepEdges ? '收起依赖线' : '依赖线' }}
        </button>
        <button class="btn" @click="toggleAllRegionTasks">
          {{ allTasksCollapsed ? '展开全部任务' : '收起全部任务' }}
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
          <!-- 顶部功能条带底板（功能模块区视觉分层） -->
          <div
            class="strip-band"
            :style="{ left: stripBand.x + 'px', top: stripBand.y + 'px', width: stripBand.w + 'px', height: stripBand.h + 'px' }"
          ><span class="strip-label">功能区</span></div>
          <svg class="edges" :width="canvasSize.w" :height="canvasSize.h">
            <path
              v-for="edge in edgePaths"
              :key="edge.id"
              :d="edge.d"
              class="edge"
              :class="{ broken: edge.broken, converge: edge.converge, consumes: edge.consumes, feed: edge.feed }"
            />
            <path v-if="wireDraft" :d="wireDraftPath" class="edge drafting" />
            <path v-if="depDraft" :d="depDraftPath" class="edge dep-drafting" />
            <template v-if="showDepEdges">
              <path
                v-for="dep in depEdges"
                :key="dep.id"
                :d="dep.d"
                class="edge dep"
                :data-dep-id="dep.id"
                @mousedown.stop
                @click="onDepEdgeClick(dep)"
              ><title>依赖：{{ dep.fromLabel }} → {{ dep.toLabel }}（点击解除）</title></path>
            </template>
          </svg>

          <!-- 泳道标题（五层流水线：源→采集→加工→区域→应用） -->
          <div
            v-for="lane in LANES"
            :key="lane.key"
            class="lane-title"
            :style="{ left: lane.x + 'px', top: laneY + 'px' }"
          >{{ lane.label }}</div>

          <!-- 数据源节点（静态展板，只读） -->
          <article
            v-for="node in sourceNodes"
            :key="node.id"
            class="node source-node"
            :style="{ left: node.x + 'px', top: node.y + 'px' }"
          >
            <b>{{ node.label }}</b>
            <span class="src-desc">{{ node.desc }}</span>
          </article>

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
            :tasks-collapsed="collapsedTaskRegions.has(node.key)"
            :busy="busy"
            @drag="startNodeDrag"
            @wire-start="startWire"
            @toggle-members="toggleRegionMembers"
            @toggle-tasks="toggleRegionTasks"
            @open-drawer="(n) => openDrawer('region', n)"
          />

          <!-- 全局节点（播报日历 / 操作记录 / 授权 / 数据修正） -->
          <GlobalNodes
            :nodes="visibleGlobalNodes"
            @drag="startNodeDrag"
            @open="onGlobalNodeClick"
          />

          <!-- 家族模块节点（区域→模块 N:1；点头部展开成员）；区域折叠后不收 -->
          <ModuleNode
            v-for="node in visibleTaskNodes"
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
        <span class="lg"><i class="line feed" /> 数据流（源→采集→加工）</span>
        <span class="lg"><i class="line dep" /> 依赖（调度门禁）</span>
        <span class="lg"><i class="line consumes" /> 日历驱动（默认收起，头部开关展开）</span>
        <span class="lg"><i class="line drafting" /> 拉线中＝归口</span>
        <span class="lg">泳道＝流水线分层 · 圆形＝聚合节点 · 顶部条带＝功能区</span>
      </div>
    </div>

    <!-- 节点抽屉（F2：宿主 + 按 kind 动态分发到 drawers/ 组件） -->
    <DrawerHost
      :drawer="drawer"
      :busy="busy"
      :month="month"
      :roster="lastRaw && lastRaw.roster"
      :region-nodes="regionNodes"
      :groups="lastRaw && lastRaw.groups"
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
.btn.btn-active {
  border-color: #0ea5e9;
  color: #0369a1;
  background: #f0f9ff;
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
.edge.feed {
  stroke: #94a3b8;
  stroke-width: 1.8;
  stroke-dasharray: 1 0;
}
/* 泳道标题与源节点 */
.lane-title {
  position: absolute;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 2px;
  color: #64748b;
  pointer-events: none;
}
.source-node {
  position: absolute;
  width: 200px;
  padding: 8px 12px;
  border: 1px dashed #cbd5e1;
  border-radius: 10px;
  background: #f8fafc;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.source-node b {
  font-size: 13px;
}
.src-desc {
  color: #94a3b8;
  font-size: 11px;
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
.line.feed {
  border-top: 1.8px solid #94a3b8;
}
.line.dep {
  border-top: 2px dashed #8b5cf6;
}
.edge.consumes {
  stroke: #0ea5e9;
  stroke-width: 1.6;
  stroke-dasharray: 3 3;
}
.line.consumes {
  border-top: 1.6px dashed #0ea5e9;
}

/* 顶部功能条带底板 */
.strip-band {
  position: absolute;
  border: 1px solid #e3e8ef;
  border-radius: 12px;
  background: linear-gradient(180deg, #f4f7fb 0%, #fafbfd 100%);
  pointer-events: none;
}
.strip-label {
  position: absolute;
  left: 12px;
  top: -9px;
  padding: 0 6px;
  background: #f7f8fa;
  font-size: 11px;
  color: #94a3b8;
  letter-spacing: 1px;
}

/* 抽屉样式在 components/flow/drawers/drawer.css（.flow-drawer 命名空间） */
</style>
