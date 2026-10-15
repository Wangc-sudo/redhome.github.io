<script setup>
/**
 * 闭环画布（2026-10-15）：把运维中心五段业务
 * 「组织 → 名册 → 目标 → 定时任务 → 记录」放在**同一平面**上。
 *
 * 每个已登记区域＝平面上的一个活节点，节点直显四要素
 * （名册数 / 目标人数与 Σ 万元 / 下游任务数 / 闭环状态）；拖拽即写，
 * 四类拖拽动作直接落库：
 *   ① 池成员 → 区域节点       ＝ 归属变更（assignMember）
 *   ② 区域成员 → 另一区域节点 ＝ 归属搬迁（moveMember，原子复合写 + confirm）
 *   ③ 成员 → 区域「名册槽」   ＝ 名册补录（addRosterEntry）
 *   ④ 名册槽条目 → 池         ＝ 名册移除（deleteRosterEntry）
 *
 * 读面并行拉既有 GET（overview / roster / pipelines），**零新增聚合读端点**；
 * 定时任务只读展示 + 深链，**绝不触发管道发布**（不碰 Nacos）。
 * 节点坐标存 localStorage（v1 不落库）。
 */
import { computed, nextTick, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import draggable from 'vuedraggable';
import {
  addRosterEntry,
  assignMember,
  deleteRosterEntry,
  getOverview,
  getPipelines,
  getRoster,
  moveMember,
} from '../api.js';
import { hasCapability, me, setFlash, flash } from '../store/me.js';

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
const nodes = ref([]);        // 区域节点（可拖拽列表：members + chips）
const poolMembers = ref([]);  // 未归类池成员卡
const month = ref('');
const loading = ref(true);
const loadError = ref('');
const busy = ref(false);      // 有写接口在途：暂停拖拽，防并发写入

const dragDisabled = computed(() => busy.value);

// -- 统一锚点（?region=<key>）--------------------------------------------
const anchorRegion = computed(() =>
  typeof route.query.region === 'string' ? route.query.region : '',
);

// -- 节点坐标（localStorage 持久化，v1 不落库）----------------------------
const POS_KEY = 'ops-web.flow.node-pos.v1';
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
    /* 隐私模式 / 配额满：坐标持久化失败不影响业务 */
  }
}

function defaultPos(index) {
  const col = index % 3;
  const row = Math.floor(index / 3);
  return { x: 16 + col * 340, y: 16 + row * 300 };
}

/** 复位某节点坐标到默认网格位（并清除本地记忆）。 */
async function resetPos(key) {
  delete nodePos.value[key];
  savePos();
  buildNodes(lastRaw.value);
  await packDefaults();
}

// -- 拖拽组（互不串门）----------------------------------------------------
// 成员卡：池 / 区域成员区 之间自由流动；名册槽只收成员、自身卡片可拉出到池。
const memberGroup = { name: 'flow-members', pull: true, put: ['flow-members'] };
const poolGroup = {
  name: 'flow-members', pull: true, put: ['flow-members', 'flow-roster'],
};
const slotGroup = { name: 'flow-roster', pull: true, put: ['flow-members'] };

// -- 数据装载 -------------------------------------------------------------
const lastRaw = ref(null);

/** 成员卡装饰：唯一键 + 类型标签 + 当前区域（搬迁用）。 */
function decorateMember(member, region) {
  return {
    ...member,
    _kind: 'member',
    _key: `m:${member.user_id}`,
    region: region || null,
  };
}

/** 名册条目 → 槽位卡（可拖出到池＝移除）。 */
function decorateChip(row) {
  return {
    ...row,
    _kind: 'roster',
    _key: `r:${row.id}`,
    roster_id: row.id,
  };
}

function pipelineCount(map, key) {
  if (!map) return null;
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : 0;
}

/** 由原始读面派生节点（members / chips / metrics / 坐标）。 */
function buildNodes(raw) {
  const { overview, roster, pipelineCounts } = raw;
  const rosterRows = (roster && roster.roster) || [];
  const targets = (roster && roster.targets) || {};
  const regions = (overview && overview.regions) || [];

  poolMembers.value = ((overview && overview.pool) || [])
    .map((member) => decorateMember(member, null));

  nodes.value = regions.map((region, index) => {
    const key = region.key;
    const members = [];
    for (const dept of region.depts || []) {
      for (const member of dept.members || []) {
        members.push(decorateMember(member, key));
      }
    }
    const chips = rosterRows
      .filter((row) => row.scope === key && Number(row.enabled) === 1)
      .map(decorateChip);
    const targetMap = targets[key] || {};
    const targetCount = Object.keys(targetMap).length;
    const targetSum = Object.values(targetMap)
      .reduce((sum, value) => sum + (Number(value) || 0), 0);
    const k = pipelineCount(pipelineCounts, key);
    const metrics = buildMetrics(members.length, chips.length, targetCount,
                                 targetSum, k);
    const pos = nodePos.value[key] || defaultPos(index);
    return {
      key,
      label: region.label || key,
      members,
      chips,
      metrics,
      chain: buildChain(chips.length, targetCount, k),
      x: pos.x,
      y: pos.y,
    };
  });
}

function buildMetrics(memberCount, rosterCount, targetCount, targetSum, k) {
  let badge = '已闭环';
  let badgeClass = 'ok';
  if (rosterCount === 0) {
    badge = '缺名册';
    badgeClass = 'warn';
  } else if (targetCount === 0) {
    badge = '缺目标';
    badgeClass = 'warn';
  } else if (!k) {
    badge = '待下游';
    badgeClass = 'warn';
  }
  return {
    memberCount, rosterCount, targetCount, targetSum,
    pipelineCount: k == null ? 0 : k,
    pipelineUnknown: k == null,
    badge, badgeClass, broken: badgeClass !== 'ok',
  };
}

/** 本区五段链路：组织（恒在）/ 名册 / 目标 / 任务 / 记录（随任务）。 */
function buildChain(rosterCount, targetCount, k) {
  return [
    { key: 'org', label: '组织', ok: true },
    { key: 'roster', label: '名册', ok: rosterCount > 0 },
    { key: 'target', label: '目标', ok: targetCount > 0 },
    { key: 'task', label: '任务', ok: !!k },
    { key: 'record', label: '记录', ok: !!k },
  ];
}

// -- 主链聚合（五段总览）--------------------------------------------------
const stageStats = computed(() => {
  let members = 0;
  let roster = 0;
  let target = 0;
  let tasks = 0;
  let withTasks = 0;
  for (const node of nodes.value) {
    members += node.metrics.memberCount;
    roster += node.metrics.rosterCount;
    target += node.metrics.targetCount;
    tasks += node.metrics.pipelineCount;
    if (node.metrics.pipelineCount > 0) withTasks += 1;
  }
  return [
    { key: 'org', label: '组织', value: members, ok: members > 0 },
    { key: 'roster', label: '名册', value: roster, ok: roster > 0 },
    { key: 'target', label: '目标', value: target, ok: target > 0 },
    { key: 'task', label: '定时任务', value: tasks, ok: tasks > 0 },
    { key: 'record', label: '记录', value: withTasks, ok: withTasks > 0 },
  ];
});

async function load({ silent = false } = {}) {
  if (!silent) loading.value = true;
  loadError.value = '';
  try {
    const [overview, roster, pipelines] = await Promise.all([
      getOverview(),
      canRoster.value ? getRoster() : Promise.resolve(null),
      canPipelines.value ? getPipelines() : Promise.resolve(null),
    ]);
    const pipelineCounts = {};
    for (const group of (pipelines && pipelines.groups) || []) {
      if (group && group.region) pipelineCounts[group.region] = group.count || 0;
    }
    month.value = (roster && roster.month) || '';
    lastRaw.value = {
      overview,
      roster,
      pipelineCounts: canPipelines.value ? pipelineCounts : null,
    };
    buildNodes(lastRaw.value);
  } catch (err) {
    loadError.value = err.message;
  } finally {
    if (!silent) loading.value = false;
  }
  await packDefaults();
  await applyAnchor();
}

function regionLabel(key) {
  const hit = nodes.value.find((node) => node.key === key);
  return hit ? hit.label : key;
}

/** 落点高亮：有 ?region= 锚点时滚动到对应节点。 */
async function applyAnchor() {
  if (!anchorRegion.value) return;
  await nextTick();
  const el = document.getElementById(`region-${anchorRegion.value}`);
  if (el) el.scrollIntoView({ block: 'center' });
}

function fmt(value) {
  const num = Number(value) || 0;
  return Number.isInteger(num) ? String(num) : num.toFixed(1);
}

// -- 拖拽落位：成员/槽位卡 → 未归类池 -------------------------------------
async function onPoolChange(evt) {
  if (!evt.added) return;
  const el = evt.added.element;
  busy.value = true;
  try {
    if (el._kind === 'roster') {
      await deleteRosterEntry({ id: el.roster_id });
      setFlash('ok', `已从名册移除「${el.person_name}」`);
    } else {
      await assignMember({
        user_id: el.user_id,
        region: null,
        dept_id: null,
        dept_name: '',
        name: el.name || null,
      });
      setFlash('ok', `已把「${el.name || el.user_id}」移出全部区域（未归类）`);
    }
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}（已回滚）`);
    await load({ silent: true });
  } finally {
    busy.value = false;
  }
}

// -- 拖拽落位：成员 → 某区域节点（归属变更 / 搬迁）------------------------
async function onNodeMembersChange(evt, node) {
  if (!evt.added) return;
  const el = evt.added.element;
  if (el._kind !== 'member') {
    await load({ silent: true });
    return;
  }
  const fromRegion = el.region || null;
  const toRegion = node.key;
  if (fromRegion === toRegion) return; // 同区排序，不落库

  // 跨区搬迁：必须二次确认（会同步增删双侧名册，影响报数资格与目标归属）。
  if (fromRegion !== null) {
    const ok = window.confirm(
      `将「${el.name || el.user_id}」从 ${regionLabel(fromRegion)} 改归属到 `
      + `${node.label}？\n\n这会同步增删双侧 person 名册，影响该成员的`
      + `报数资格与当月目标归属。`,
    );
    if (!ok) {
      setFlash('ok', '已取消搬迁');
      await load({ silent: true });
      return;
    }
  }

  busy.value = true;
  try {
    if (fromRegion === null) {
      await assignMember({
        user_id: el.user_id,
        region: toRegion,
        dept_id: el.dept_id || null,
        dept_name: el.dept_name || '',
        name: el.name || null,
      });
      setFlash('ok', `已把「${el.name || el.user_id}」归入 ${node.label}`);
    } else {
      const res = await moveMember({
        user_id: el.user_id,
        to_region: toRegion,
        person_name: el.name || null,
        dept_name: el.dept_name || '',
        from_region: fromRegion,
        note: '',
      });
      const moved = (res && res.roster) || {};
      const tail = `${moved.to_added ? '（已补新名册）' : ''}`
        + `${moved.from_removed ? '（已清旧名册）' : ''}`;
      setFlash('ok',
        `已搬迁「${el.name || el.user_id}」到 ${node.label}${tail}`);
    }
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}（已回滚）`);
    await load({ silent: true });
  } finally {
    busy.value = false;
  }
}

// -- 拖拽落位：成员 → 某区域「名册槽」（补录）----------------------------
async function onSlotChange(evt, node) {
  if (!evt.added) return;
  const el = evt.added.element;
  if (el._kind !== 'member') {
    await load({ silent: true });
    return;
  }
  busy.value = true;
  try {
    await addRosterEntry({
      scope: node.key,
      entity_type: 'person',
      entity_key: el.dept_name || node.label,
      person_name: el.name || el.user_id,
      role: 'owner',
      note: '',
    });
    setFlash('ok', `已把「${el.name || el.user_id}」补录到 ${node.label} 名册`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}（已回滚）`);
    await load({ silent: true });
  } finally {
    busy.value = false;
  }
}

// -- 节点位置拖拽（头部拖动＝改坐标，存 localStorage）---------------------
function startNodeDrag(evt, node) {
  if (busy.value || evt.button !== 0) return;
  // 仅左键；避免与节点内卡片拖拽/链接点击冲突（头部为专用拖拽柄）。
  const originX = node.x;
  const originY = node.y;
  const startX = evt.clientX;
  const startY = evt.clientY;

  function onMove(moveEvt) {
    node.x = Math.max(0, originX + moveEvt.clientX - startX);
    node.y = Math.max(0, originY + moveEvt.clientY - startY);
  }
  function onUp() {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    nodePos.value[node.key] = { x: node.x, y: node.y };
    savePos();
  }
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}

const planeHeight = computed(() => {
  let max = 360;
  for (const node of nodes.value) max = Math.max(max, node.y + nodeHeight(node) + 16);
  return max;
});

/** 节点实测高度（渲染后测量；未测到回退 320）。 */
function nodeHeight(node) {
  return node.height || 320;
}

/**
 * 默认网格位：按**实测高度**逐列下推，避免大节点（成员/名册多）盖住下方节点。
 * 用户已拖过的节点（nodePos 有记录）不参与排布。
 */
async function packDefaults() {
  if (!nodes.value.length) return;
  await nextTick();
  for (const node of nodes.value) {
    const el = document.getElementById(`region-${node.key}`);
    if (el) node.height = el.offsetHeight || 320;
  }
  await nextTick();
  const cols = [16, 16, 16];
  let dirty = false;
  for (const node of nodes.value) {
    if (nodePos.value[node.key]) continue;
    const col = cols.indexOf(Math.min(...cols));
    const x = 16 + col * 344;
    const y = cols[col];
    if (node.x !== x || node.y !== y) dirty = true;
    node.x = x;
    node.y = y;
    cols[col] = y + nodeHeight(node) + 24;
  }
  if (dirty) await nextTick();
}

// -- 生命周期 -------------------------------------------------------------
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
          组织 → 名册 → 目标 → 定时任务 → 记录 一平面总览。拖拽成员卡即改归属、
          跨区拖＝搬迁（同步双侧名册），拖入「名册槽」＝补录名册，槽位卡拖回池＝移除。
          定时任务只读，画布**不触发**管道发布。
        </p>
      </div>
      <div class="head-actions">
        <span v-if="month" class="month">目标月份 {{ month }}</span>
        <button class="btn" :disabled="loading" @click="load()">刷新</button>
      </div>
    </header>

    <transition name="flash">
      <div v-if="flash" class="flash" :class="flash.type">{{ flash.text }}</div>
    </transition>

    <div v-if="loading" class="state">加载中…</div>
    <div v-else-if="loadError" class="state error">{{ loadError }}</div>

    <template v-else>
      <!-- 五段业务主链（断链段虚线） -->
      <section class="chain">
        <div class="chain-row">
          <template v-for="(stage, index) in stageStats" :key="stage.key">
            <div class="stage" :class="{ broken: !stage.ok }">
              <span class="stage-name">{{ stage.label }}</span>
              <span class="stage-value">{{ stage.value }}</span>
            </div>
            <svg
              v-if="index < stageStats.length - 1"
              class="arrow"
              :class="{ dashed: !stageStats[index + 1].ok }"
              width="42"
              height="18"
              viewBox="0 0 42 18"
              aria-hidden="true"
            >
              <line x1="2" y1="9" x2="32" y2="9" />
              <polygon points="32,4 41,9 32,14" />
            </svg>
          </template>
        </div>
      </section>

      <div class="grid">
        <!-- 未归类池 -->
        <aside class="col pool-col">
          <div class="col-head">
            <h2>未归类池</h2>
            <span class="count">{{ poolMembers.length }}</span>
          </div>
          <p class="hint">拖入区域节点＝归入该区；拖入名册槽＝补录名册。</p>
          <draggable
            class="pool-drop"
            :list="poolMembers"
            :group="poolGroup"
            item-key="_key"
            :disabled="dragDisabled"
            :animation="150"
            @change="onPoolChange"
          >
            <template #item="{ element: member }">
              <div class="card">
                <span class="card-name">{{ member.name || member.user_id || '—' }}</span>
                <span class="card-sub">{{ member.dept_name || member.user_id || '' }}</span>
              </div>
            </template>
          </draggable>
        </aside>

        <!-- 区域节点平面 -->
        <section class="plane-wrap">
          <p v-if="!nodes.length" class="state">尚无已登记区域。</p>
          <div v-else class="plane" :style="{ height: planeHeight + 'px' }">
            <article
              v-for="node in nodes"
              :key="node.key"
              :id="`region-${node.key}`"
              class="node"
              :class="{ 'anchor-hit': anchorRegion === node.key }"
              :style="{ left: node.x + 'px', top: node.y + 'px' }"
            >
              <div
                class="node-head"
                title="拖动头部＝移动节点（坐标存本机）"
                @mousedown.prevent="startNodeDrag($event, node)"
              >
                <h3>{{ node.label }}</h3>
                <span class="badge" :class="node.metrics.badgeClass">
                  {{ node.metrics.badge }}
                </span>
                <button
                  class="btn mini ghost"
                  title="复位该节点位置"
                  @mousedown.stop
                  @click.stop="resetPos(node.key)"
                >
                  复位
                </button>
              </div>

              <div class="metrics">
                <router-link
                  class="metric"
                  :to="{ path: '/roster', query: { region: node.key } }"
                  title="该区名册（点开名册页并高亮）"
                >
                  <b>{{ node.metrics.rosterCount }}</b><span>名册</span>
                </router-link>
                <router-link
                  class="metric"
                  :to="{ path: '/roster', query: { region: node.key } }"
                  title="该区当月目标人数（万元口径）"
                >
                  <b>{{ node.metrics.targetCount }}</b><span>目标人</span>
                </router-link>
                <span class="metric" title="该区当月个人目标合计（万元）">
                  <b>{{ fmt(node.metrics.targetSum) }}</b><span>Σ 万元</span>
                </span>
                <router-link
                  v-if="node.metrics.pipelineCount > 0"
                  class="metric"
                  :to="{ path: '/pipelines', query: { region: node.key } }"
                  title="该区下游定时任务（点开并高亮）"
                >
                  <b>{{ node.metrics.pipelineCount }}</b><span>下游任务</span>
                </router-link>
                <span
                  v-else
                  class="metric is-zero"
                  title="该区暂无下游定时任务"
                >
                  <b>0</b><span>下游任务</span>
                </span>
                <router-link
                  class="metric"
                  :to="{ path: '/org', query: { region: node.key } }"
                  title="该区组织成员（点开组织页并高亮）"
                >
                  <b>{{ node.metrics.memberCount }}</b><span>成员</span>
                </router-link>
                <router-link class="metric" to="/records" title="六源审计记录">
                  <b>·</b><span>记录</span>
                </router-link>
              </div>

              <!-- 本区五段小闭环（缺口段虚线） -->
              <div class="mini-chain">
                <template v-for="(seg, index) in node.chain" :key="seg.key">
                  <span class="mini-chip" :class="{ on: seg.ok }">{{ seg.label }}</span>
                  <span
                    v-if="index < node.chain.length - 1"
                    class="mini-gap"
                    :class="{ dashed: !node.chain[index + 1].ok }"
                  />
                </template>
              </div>

              <!-- 名册槽（拖入成员＝补录；槽位卡拖回池＝移除） -->
              <div v-if="canRoster" class="slot">
                <div class="slot-head">
                  名册槽 <span class="count">{{ node.chips.length }}</span>
                </div>
                <draggable
                  class="slot-drop"
                  :list="node.chips"
                  :group="slotGroup"
                  item-key="_key"
                  :disabled="dragDisabled"
                  :animation="150"
                  @change="onSlotChange($event, node)"
                >
                  <template #item="{ element: chip }">
                    <span
                      class="chip-roster"
                      :title="`${chip.person_name}（${chip.role || 'owner'}）拖到左侧池＝移除`"
                    >
                      {{ chip.person_name }}
                    </span>
                  </template>
                </draggable>
              </div>

              <!-- 区域成员（拖到其它区＝搬迁、拖到名册槽＝补录、拖到池＝移出） -->
              <draggable
                class="node-members"
                :list="node.members"
                :group="memberGroup"
                item-key="_key"
                :disabled="dragDisabled"
                :animation="150"
                @change="onNodeMembersChange($event, node)"
              >
                <template #item="{ element: member }">
                  <span
                    class="chip"
                    :id="`member-${member.user_id}`"
                    :title="`${member.user_id}｜拖到其它区域＝搬迁、拖到名册槽＝补录`"
                  >
                    {{ member.name || member.user_id }}
                  </span>
                </template>
              </draggable>
            </article>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.page {
  max-width: 1440px;
  margin: 0 auto;
  padding: 20px;
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
  max-width: 820px;
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
.btn.ghost {
  border-color: transparent;
  color: #4b5563;
}
.btn.mini {
  padding: 3px 8px;
  font-size: 12px;
}

.flash {
  margin: 12px 0;
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

/* 五段主链 */
.chain {
  margin-top: 16px;
  padding: 12px 14px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fff;
}
.chain-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.stage {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 92px;
  padding: 8px 12px;
  border: 1px solid #c7d2fe;
  border-radius: 8px;
  background: #eef2ff;
}
.stage.broken {
  border-color: #fde68a;
  background: #fffbeb;
}
.stage-name {
  font-size: 12px;
  color: #4b5563;
}
.stage-value {
  font-size: 18px;
  font-weight: 600;
}
.arrow {
  flex: 0 0 auto;
}
.arrow line,
.arrow polygon {
  fill: #10b981;
  stroke: #10b981;
  stroke-width: 2;
}
.arrow.dashed line {
  fill: none;
  stroke: #f59e0b;
  stroke-dasharray: 5 4;
}
.arrow.dashed polygon {
  fill: #f59e0b;
  stroke: #f59e0b;
}

/* 两栏 */
.grid {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: 16px;
  margin-top: 16px;
  align-items: start;
}
@media (max-width: 960px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
.col {
  padding: 12px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fff;
}
.col-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.col-head h2 {
  margin: 0;
  font-size: 15px;
}
.count {
  display: inline-block;
  min-width: 20px;
  padding: 1px 6px;
  border-radius: 10px;
  background: #f3f4f6;
  color: #4b5563;
  font-size: 12px;
  text-align: center;
}
.hint {
  margin: 6px 0 10px;
  color: #9ca3af;
  font-size: 12px;
  line-height: 1.4;
}
.pool-drop {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 120px;
}
.card {
  display: flex;
  flex-direction: column;
  padding: 6px 9px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #f9fafb;
  cursor: grab;
}
.card-name {
  font-size: 13px;
}
.card-sub {
  color: #9ca3af;
  font-size: 11px;
}

/* 平面 */
.plane-wrap {
  min-width: 0;
}
.plane {
  position: relative;
  overflow: auto;
  border: 1px dashed #d1d5db;
  border-radius: 10px;
  background:
    radial-gradient(#e5e7eb 1px, transparent 1px) 0 0 / 22px 22px,
    #fcfcfd;
}
.node {
  position: absolute;
  width: 320px;
  padding: 10px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.06);
}
.node.anchor-hit {
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.18);
}
.node-head {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: move;
}
.node-head h3 {
  margin: 0;
  font-size: 15px;
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

.metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 8px 0 6px;
}
.metric {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 58px;
  padding: 4px 8px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #f9fafb;
  color: #1f2933;
  text-decoration: none;
  font-size: 11px;
}
.metric b {
  font-size: 15px;
}
.metric span {
  color: #6b7280;
}
a.metric:hover {
  border-color: #93c5fd;
  background: #eff6ff;
}
.metric.is-zero {
  color: #9ca3af;
}
.metric.is-zero b {
  color: #9ca3af;
}

/* 本区五段小闭环 */
.mini-chain {
  display: flex;
  align-items: center;
  gap: 3px;
  margin: 4px 0 8px;
}
.mini-chip {
  padding: 1px 6px;
  border-radius: 10px;
  background: #f3f4f6;
  color: #9ca3af;
  font-size: 11px;
}
.mini-chip.on {
  background: #ecfdf5;
  color: #047857;
}
.mini-gap {
  width: 12px;
  border-top: 2px solid #10b981;
}
.mini-gap.dashed {
  border-top: 2px dashed #f59e0b;
}

/* 名册槽 */
.slot {
  margin-bottom: 8px;
  padding: 6px 8px;
  border: 1px dashed #c7d2fe;
  border-radius: 8px;
  background: #f8faff;
}
.slot-head {
  color: #4b5563;
  font-size: 12px;
  margin-bottom: 4px;
}
.slot-drop {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  min-height: 24px;
}
.chip-roster {
  padding: 2px 7px;
  border: 1px solid #a7f3d0;
  border-radius: 10px;
  background: #ecfdf5;
  color: #047857;
  font-size: 12px;
  cursor: grab;
}

/* 区域成员 */
.node-members {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  min-height: 26px;
}
.chip {
  padding: 3px 8px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #f9fafb;
  font-size: 12px;
  cursor: grab;
}
</style>
