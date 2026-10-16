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
import { computed, nextTick, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import {
  assignMember,
  getOverview,
  getPipelines,
  getRoster,
  moveMember,
  runPipelineOnce,
  togglePipeline,
  updatePipelineDeps,
} from '../api.js';
import { hasCapability, isAdmin, me, setFlash, flash } from '../store/me.js';
// 建图/布局纯逻辑已抽出（2026-10-16 F1）：flow/graph.js + flow/layout.js；
// 抽屉已组件化（2026-10-17 F2）：components/flow/drawers/ + flow/useDrawer.js，
// 本文件只保留画布状态、拉线/拖拽交互与装载编排。
import { buildGraph as buildGraphModel, fmt } from '../flow/graph.js';
import { globalNodeKind, useDrawer } from '../flow/useDrawer.js';
import DrawerHost from '../components/flow/drawers/DrawerHost.vue';
import {
  AGG_SIZE,
  GLOBAL_H,
  GLOBAL_W,
  POOL_W,
  REGION_W,
  TASK_W,
  TOP_Y,
  buildDepEdges,
  buildEdgePaths,
  canvasSize as canvasSizeFor,
  edgePath,
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

// -- 视口（缩放/平移）-------------------------------------------------------
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

// -- 节点坐标持久化 -----------------------------------------------------------
const POS_KEY = 'ops-web.flow.node-pos.v3';
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
    /* 坐标持久化失败不影响业务 */
  }
}
function resetAllPos() {
  nodePos.value = {};
  savePos();
  layout();
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

function startNodeDrag(evt, node) {
  if (evt.button !== 0 || busy.value) return;
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

// -- 拉线（成员 → 区域/部门；2026-10-16 部门粒度化）----------------------------
// 语义配对：成员口 → 区域口＝归口该区（部门取通讯录原值）；
// 成员口 → 部门口＝归口该区该部门；区域成员口 → 他区部门/区域口＝搬迁
// （moveMember，confirm+双侧名册同步）；区域成员口 → 本区部门口＝区内调部门。
const wireDraft = ref(null);   // {member, x1, y1, x2, y2}
const poolNode = ref({ id: 'pool', x: 40, y: TOP_Y });

function startWire(evt, member) {
  if (evt.button !== 0 || busy.value) return;
  if (!canPipelines.value && !canRoster.value) return;
  evt.stopPropagation();
  evt.preventDefault();
  const canvasEl = evt.target.closest('.canvas');
  const rect = canvasEl.getBoundingClientRect();
  // 起点=被按下的端口圆心（画布坐标），不再绑死池节点几何
  const portRect = evt.target.getBoundingClientRect();
  const x1 = (portRect.left + portRect.width / 2 - rect.left - panX.value) / zoom.value;
  const y1 = (portRect.top + portRect.height / 2 - rect.top - panY.value) / zoom.value;
  wireDraft.value = { member, x1, y1, x2: x1, y2: y1 };
  function toCanvas(moveEvt) {
    return {
      x: (moveEvt.clientX - rect.left - panX.value) / zoom.value,
      y: (moveEvt.clientY - rect.top - panY.value) / zoom.value,
    };
  }
  function onMove(moveEvt) {
    const p = toCanvas(moveEvt);
    wireDraft.value.x2 = p.x;
    wireDraft.value.y2 = p.y;
  }
  function onUp(upEvt) {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    const draft = wireDraft.value;
    wireDraft.value = null;
    const hit = document.elementFromPoint(upEvt.clientX, upEvt.clientY);
    if (!hit || !hit.closest) return;
    const deptPort = hit.closest('[data-dept-in]');
    if (deptPort) {
      dropWire(deptPort.getAttribute('data-dept-in'), draft.member);
      return;
    }
    const regionPort = hit.closest('[data-region-in]');
    if (regionPort) {
      dropWire(regionPort.getAttribute('data-region-in'), draft.member);
    }
  }
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}

function regionLabel(key) {
  const hit = regionNodes.value.find((n) => n.key === key);
  return hit ? hit.label : key;
}

/** 落点解析："regionKey"（区域口）或 "regionKey|deptId|deptName"（部门口）。 */
async function dropWire(target, member) {
  const [regionKey, deptId, deptName] = target.split('|');
  const region = regionNodes.value.find((n) => n.key === regionKey);
  if (!region) return;
  const fromRegion = member.region || null;   // 区域成员卡带 region 标记
  const name = member.name || member.user_id;
  const deptText = deptName ? `·${deptName}` : '';
  const moving = fromRegion && fromRegion !== regionKey;

  const ok = window.confirm(moving
    ? `将「${name}」从 ${regionLabel(fromRegion)} 搬迁到 ${region.label}${deptText}？\n\n`
      + '跨区搬迁会同步增删双侧名册，影响报数资格与当月目标归属。'
    : `将「${name}」归入 ${region.label}${deptText}？`);
  if (!ok) return;

  busy.value = true;
  try {
    if (moving) {
      await moveMember({
        user_id: member.user_id,
        to_region: regionKey,
        person_name: member.name || null,
        dept_name: deptName || member.dept_name || '',
        from_region: fromRegion,
        note: '',
      });
      setFlash('ok', `已搬迁「${name}」到 ${region.label}${deptText}`);
    } else {
      await assignMember({
        user_id: member.user_id,
        region: regionKey,
        dept_id: deptId || member.dept_id || null,
        dept_name: deptName || member.dept_name || '',
        name: member.name || null,
      });
      setFlash('ok', `已把「${name}」归入 ${region.label}${deptText}`);
    }
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

const wireDraftPath = computed(() => {
  const w = wireDraft.value;
  return w ? edgePath(w.x1, w.y1, w.x2, w.y2) : '';
});

// -- 拉线（模块 → 模块 ＝ 任务依赖；2026-10-16「拉线改后端」）---------------
// 语义：从「先跑」模块的输出端口拉线到「后跑」模块的输入端口，
// 目标模块全部成员的 depends_on 追加源模块全部成员（逐成员调 deps API）。
const depDraft = ref(null);    // {fromModule, x1, y1, x2, y2}

function startDepWire(evt, node) {
  if (evt.button !== 0 || busy.value || !canPipelines.value) return;
  evt.stopPropagation();
  evt.preventDefault();
  const x1 = node.x + TASK_W;
  const y1 = node.y + moduleH(node) / 2;
  const canvasEl = evt.target.closest('.canvas');
  const rect = canvasEl.getBoundingClientRect();
  depDraft.value = { fromModule: node, x1, y1, x2: x1, y2: y1 };
  function onMove(moveEvt) {
    depDraft.value.x2 = (moveEvt.clientX - rect.left - panX.value) / zoom.value;
    depDraft.value.y2 = (moveEvt.clientY - rect.top - panY.value) / zoom.value;
  }
  function onUp(upEvt) {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    const draft = depDraft.value;
    depDraft.value = null;
    const hit = document.elementFromPoint(upEvt.clientX, upEvt.clientY);
    const port = hit && hit.closest ? hit.closest('[data-module-in]') : null;
    if (!port) return;
    const targetId = port.getAttribute('data-module-in');
    const target = taskNodes.value.find((n) => n.id === targetId);
    if (target) dropDepWire(draft.fromModule, target);
  }
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}

async function dropDepWire(fromModule, toModule) {
  if (fromModule.id === toModule.id) return;
  const sourceIds = fromModule.services.map((s) => s.service_id);
  const ok = window.confirm(
    `建立依赖：「${toModule.label}」的全部任务 依赖 「${fromModule.label}」？\n`
    + `（${toModule.services.map((s) => s.service_id).join('、')} `
    + `depends_on += ${sourceIds.join('、')}）`,
  );
  if (!ok) return;
  busy.value = true;
  try {
    for (const service of toModule.services) {
      const next = [...new Set([...(service.depends_on || []), ...sourceIds])];
      await updatePipelineDeps({ service_id: service.service_id, depends_on: next });
    }
    setFlash('ok', `已建立依赖：${toModule.label} ← ${fromModule.label}`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `建立依赖失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

/** 点选依赖线＝解除（目标模块成员 depends_on 移除源模块成员）。 */
async function onDepEdgeClick(dep) {
  const ok = window.confirm(
    `解除依赖：「${dep.toLabel}」 不再依赖 「${dep.fromLabel}」？`,
  );
  if (!ok) return;
  busy.value = true;
  try {
    for (const service of dep.toModule.services) {
      const next = (service.depends_on || [])
        .filter((d) => !dep.sourceIds.includes(d));
      await updatePipelineDeps({ service_id: service.service_id, depends_on: next });
    }
    setFlash('ok', `已解除依赖：${dep.toLabel} ✕ ${dep.fromLabel}`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `解除依赖失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

const depDraftPath = computed(() => {
  const w = depDraft.value;
  return w ? edgePath(w.x1, w.y1, w.x2, w.y2) : '';
});

// -- 抽屉（F2：状态机 + 组件化，实现见 components/flow/drawers/）-----------------
const { drawer, openDrawer, closeDrawer } = useDrawer();
const reloadSilent = () => load({ silent: true });

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

async function onToggleTask(node) {
  const target = !node.service.enabled;
  busy.value = true;
  try {
    await togglePipeline({ service_id: node.service.service_id, enabled: target });
    setFlash('ok', `「${node.service.service_label}」已${target ? '启用' : '停用'}`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `开关失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onRunOnce(node) {
  const ok = window.confirm(
    `立即运行一次「${node.service.service_label}」？\n（手动触发不受当日去重限制，会真实发播报/写页面）`,
  );
  if (!ok) return;
  busy.value = true;
  try {
    await runPipelineOnce({ service_id: node.service.service_id });
    setFlash('ok', `「${node.service.service_label}」已提交运行，流水见任务抽屉`);
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

/** 模块节点高度（纯函数在 flow/layout.js，此处注入当前展开态）。 */
function moduleH(node) {
  return moduleNodeH(node, expandedModules.value);
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
  await nextTick();
  const node = regionNodes.value.find((n) => n.key === anchorRegion.value);
  if (!node) return;
  panX.value = 260 - node.x * zoom.value;
  panY.value = 120 - node.y * zoom.value;
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
        <button class="btn" @click="resetAllPos">复位布局</button>
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
          <article
            class="node pool-node"
            :style="{ left: poolNode.x + 'px', top: poolNode.y + 'px', width: POOL_W + 'px' }"
          >
            <div class="node-head" title="拖动移动节点" @mousedown="startNodeDrag($event, poolNode)">
              <h3>未归类池</h3>
              <span class="badge plain">{{ poolMembers.length }}</span>
            </div>
            <p class="pool-hint">从成员右侧端口拉线到区域节点＝归口</p>
            <div
              v-for="(member, index) in poolMembers"
              :key="member.user_id"
              class="pool-row"
            >
              <span class="pool-name">{{ member.name || member.user_id }}</span>
              <span class="pool-dept">{{ member.dept_name || '' }}</span>
              <span
                class="port out member-port"
                :class="{ busy }"
                title="拉线到区域/部门端口＝归口"
                @mousedown="startWire($event, member)"
              />
            </div>
            <p v-if="!poolMembers.length" class="pool-hint">全员已归口</p>
          </article>

          <!-- 区域节点（聚合=圆形，业务=矩形） -->
          <article
            v-for="node in regionNodes"
            :key="node.id"
            :id="`flow-${node.key}`"
            class="node region-node"
            :class="{ 'anchor-hit': anchorRegion === node.key, 'agg-node': node.isAggregate }"
            :style="node.isAggregate
              ? { left: node.x + 'px', top: node.y + 'px', width: AGG_SIZE + 'px', height: AGG_SIZE + 'px' }
              : { left: node.x + 'px', top: node.y + 'px', width: REGION_W + 'px' }"
          >
            <span v-if="!node.isAggregate" class="port in-region" :data-region-in="node.key" title="成员归口输入" />
            <span v-else class="port in-region agg-in" title="汇聚输入（组成桶汇入）" />
            <!-- 聚合节点：圆形内容居中 -->
            <template v-if="node.isAggregate">
              <div class="agg-body" @mousedown="startNodeDrag($event, node)">
                <h3>{{ node.label }}</h3>
                <span class="badge agg">{{ node.badge.text }}</span>
                <div class="agg-metrics">
                  <b>{{ node.rosterCount }}</b><span>名册</span>
                  <b>{{ fmt(node.targetSum) }}</b><span>Σ 万元</span>
                </div>
                <button class="btn mini ghost" @mousedown.stop @click="openDrawer('region', node)">详情</button>
              </div>
            </template>
            <template v-else>
              <div class="node-head" title="拖动移动节点" @mousedown="startNodeDrag($event, node)">
                <h3>{{ node.label }}</h3>
                <span class="badge" :class="node.badge.cls">{{ node.badge.text }}</span>
              </div>
              <div class="metrics" v-if="!node.isPlatform">
                <span class="metric"><b>{{ node.rosterCount }}</b><span>名册</span></span>
                <span class="metric"><b>{{ node.targetCount }}</b><span>目标人</span></span>
                <span class="metric"><b>{{ fmt(node.targetSum) }}</b><span>Σ 万元</span></span>
                <span class="metric"><b>{{ node.memberCount }}</b><span>成员</span></span>
              </div>
              <!-- 部门槽（部门输入端口：成员拉线至此=归口到该部门） -->
              <div v-if="node.depts && node.depts.length" class="dept-slot">
                <div
                  v-for="dept in node.depts"
                  :key="dept.dept_id || dept.dept_name"
                  class="dept-row"
                >
                  <span
                    class="port in-dept"
                    :data-dept-in="`${node.key}|${dept.dept_id}|${dept.dept_name}`"
                    title="成员拉线至此＝归口到该部门"
                  />
                  <span class="dept-name">{{ dept.dept_name || '（无 ID 部门）' }}</span>
                  <span class="dept-count">{{ dept.memberCount }}</span>
                </div>
              </div>
              <!-- 成员行（展开后每人一个输出端口：拉到他区=搬迁、本区部门=调部门） -->
              <div class="member-slot">
                <button class="btn mini ghost expand-members" @click="toggleRegionMembers(node)">
                  {{ expandedRegions.has(node.id) ? '收起成员 ⌃' : `成员 ${node.memberCount} ⌄` }}
                </button>
                <template v-if="expandedRegions.has(node.id)">
                  <div
                    v-for="member in node.members"
                    :key="member.user_id"
                    class="member-row-wire"
                  >
                    <span class="member-name">{{ member.name || member.user_id }}</span>
                    <span class="member-dept">{{ member.dept_name }}</span>
                    <span
                      class="port out member-port"
                      :class="{ busy }"
                      title="拉到他区/部门口＝搬迁；本区部门口＝调部门"
                      @mousedown="startWire($event, member)"
                    />
                  </div>
                </template>
              </div>
              <div class="node-foot">
                <span class="task-count">下游任务 {{ node.taskCount }}</span>
                <button class="btn mini ghost" @click="openDrawer('region', node)">详情</button>
              </div>
            </template>
            <span class="port out" title="驱动下游任务" />
          </article>

          <!-- 全局节点（播报日历 / 操作记录 / 授权） -->
          <article
            v-for="node in visibleGlobalNodes"
            :key="node.id"
            class="node global-node"
            :style="{ left: node.x + 'px', top: node.y + 'px', width: GLOBAL_W + 'px', height: GLOBAL_H + 'px' }"
          >
            <div class="node-head" title="拖动移动节点" @mousedown="startNodeDrag($event, node)">
              <h4>{{ node.label }}</h4>
            </div>
            <button class="global-open" @click="onGlobalNodeClick(node)">
              {{ node.desc }} →
            </button>
          </article>

          <!-- 家族模块节点（区域→模块 N:1；点头部展开成员） -->
          <article
            v-for="node in taskNodes"
            :key="node.id"
            class="node task-node module-node"
            :class="{ broken: node.brokenCount > 0, expanded: expandedModules.has(node.id) }"
            :style="{ left: node.x + 'px', top: node.y + 'px', width: TASK_W + 'px' }"
          >
            <span class="port in module-in" :data-module-in="node.id" title="依赖输入（拉线至此＝本模块依赖来源模块）" />
            <span
              class="port out module-out"
              :class="{ busy }"
              title="依赖输出（拉线到别的模块＝对方依赖本模块）"
              @mousedown="startDepWire($event, node)"
            />
            <div class="node-head module-head" title="拖动移动；点击展开/收起成员" @mousedown="startNodeDrag($event, node)">
              <h4>{{ node.label }}</h4>
              <span class="badge" :class="node.brokenCount ? 'warn' : 'ok'">
                {{ node.services.length - node.brokenCount }}/{{ node.services.length }} 正常
              </span>
              <button
                class="btn mini ghost expand-btn"
                @mousedown.stop
                @click.stop="toggleModule(node)"
              >{{ expandedModules.has(node.id) ? '收起 ⌃' : '展开 ⌄' }}</button>
            </div>
            <div v-if="expandedModules.has(node.id)" class="module-members">
              <div
                v-for="service in node.services"
                :key="service.service_id"
                class="member-row"
                :class="{ disabled: !service.enabled }"
              >
                <div class="member-main">
                  <b>{{ service.service_label }}</b>
                  <span class="member-sched">
                    {{ service.schedule_zh || service.schedule || '—' }}
                    <template v-if="service.latest"> · {{ service.latest.status_label }}</template>
                  </span>
                </div>
                <div class="member-actions">
                  <button
                    class="switch"
                    :class="{ on: service.enabled }"
                    :disabled="busy"
                    :title="service.enabled ? '点击停用' : '点击启用'"
                    @click="onToggleTask({ service })"
                  ><i /></button>
                  <button class="btn mini" :disabled="busy" @click="onRunOnce({ service })">运行一次</button>
                  <button class="btn mini ghost" @click="openDrawer('task', { service })">详情</button>
                </div>
              </div>
            </div>
          </article>
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

/* 聚合节点（圆形）：与矩形业务节点视觉分层 */
.node.agg-node {
  border-radius: 50%;
  border: 2px solid #6366f1;
  background: radial-gradient(circle at 35% 30%, #eef2ff, #e0e7ff);
  box-shadow: 0 2px 8px rgba(99, 102, 241, 0.18);
}
.agg-body {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  cursor: move;
  user-select: none;
  text-align: center;
}
.agg-body h3 {
  margin: 0;
  font-size: 16px;
}
.agg-metrics {
  display: flex;
  flex-direction: column;
  font-size: 11px;
  color: #4b5563;
}
.agg-metrics b {
  font-size: 15px;
  color: #1f2933;
}
.badge.agg {
  background: #e0e7ff;
  color: #4338ca;
}
.port.agg-in {
  background: #6366f1;
}

/* 部门槽（输入端口） */
.dept-slot {
  margin: 2px 10px 4px;
  padding: 4px 0;
  border-top: 1px dashed #eef0f3;
}
.dept-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 26px;
  padding-left: 14px;
  font-size: 12px;
}
.dept-name {
  color: #374151;
}
.dept-count {
  margin-left: auto;
  color: #9ca3af;
  font-size: 11px;
}
.port.in-dept {
  left: 0;
  top: 8px;
  width: 8px;
  height: 8px;
  background: #2563eb;
  cursor: crosshair;
}

/* 成员行（输出端口） */
.member-slot {
  padding: 0 10px 4px;
}
.expand-members {
  margin: 2px 0;
}
.member-row-wire {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 26px;
  font-size: 12px;
}
.member-name {
  font-weight: 600;
}
.member-dept {
  color: #9ca3af;
  font-size: 11px;
}
.member-row-wire .member-port {
  right: -6px;
  top: 8px;
  margin-top: 0;
}
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
.port.module-out {
  background: #8b5cf6;
  cursor: crosshair;
}
.port.module-out.busy {
  opacity: 0.4;
  pointer-events: none;
}
.port.module-in {
  background: #8b5cf6;
  cursor: crosshair;
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
  font-size: 11px;
}
.metric b {
  font-size: 14px;
}
.metric span {
  color: #6b7280;
}
.node-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 2px 10px 8px;
  font-size: 11px;
  color: #9ca3af;
}

/* 池节点 */
.pool-node {
  padding-bottom: 8px;
}
.pool-hint {
  margin: 0;
  padding: 0 10px 6px;
  color: #9ca3af;
  font-size: 11px;
}
.pool-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 10px;
  font-size: 12px;
}
.pool-name {
  font-weight: 600;
}
.pool-dept {
  color: #9ca3af;
  font-size: 11px;
}
.member-port {
  right: -6px;
  cursor: crosshair;
}
.member-port.busy {
  opacity: 0.4;
  pointer-events: none;
}

/* 任务节点 */
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
.task-meta .latest {
  color: #9ca3af;
}
.task-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 10px 2px;
}

/* 家族模块节点 */
.module-node {
  border-left: 3px solid #818cf8;
}
.module-node.broken {
  border-left-color: #f59e0b;
}
.module-head {
  cursor: move;
}
.module-head h4 {
  flex: 1;
}
.expand-btn {
  flex: 0 0 auto;
}
.module-members {
  border-top: 1px dashed #eef0f3;
  padding: 4px 8px 6px;
}
.member-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 2px;
  border-bottom: 1px solid #f6f7f9;
}
.member-row:last-child {
  border-bottom: none;
}
.member-row.disabled {
  opacity: 0.6;
}
.member-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.member-main b {
  font-size: 12px;
}
.member-sched {
  font-size: 11px;
  color: #9ca3af;
}
.member-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
}

/* 开关 */
.switch {
  width: 34px;
  height: 18px;
  border: 1px solid #d1d5db;
  border-radius: 9px;
  background: #e5e7eb;
  cursor: pointer;
  position: relative;
  padding: 0;
}
.switch i {
  position: absolute;
  left: 2px;
  top: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #fff;
  transition: left 0.15s ease;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
}
.switch.on {
  background: #10b981;
  border-color: #10b981;
}
.switch.on i {
  left: 18px;
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
.pool-row .port.out {
  top: 10px;
  margin-top: 0;
}
.port.in {
  left: -6px;
  top: 35px;
  background: #64748b;
}
.port.in-region {
  left: -6px;
  top: 50%;
  margin-top: -5px;
  background: #2563eb;
  cursor: crosshair;
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

/* 抽屉样式已抽到 components/flow/drawers/drawer.css（.flow-drawer 命名空间） */
.global-node {
  padding-bottom: 4px;
  border-style: dashed;
  background: #fdfdff;
}
.global-open {
  display: block;
  margin: 0 10px;
  padding: 2px 0 6px;
  border: none;
  background: none;
  color: #2563eb;
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}
</style>
