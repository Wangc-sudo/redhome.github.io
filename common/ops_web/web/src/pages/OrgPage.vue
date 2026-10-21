<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import draggable from 'vuedraggable';
import {
  addDept,
  addRegion,
  assignMember,
  deleteDept,
  deleteOverride,
  deleteRegion,
  disableDept,
  disableRegion,
  getOverview,
  getPipelines,
  getProxyGrants,
  grantProxy,
  toggleProxy,
  deleteProxy,
  resetOverrides,
  toggleOverride,
} from '../api.js';
import { hasCapability, setFlash } from '../store/me.js';
import TargetEditor from '../components/TargetEditor.vue';

const route = useRoute();

// -- 数据状态 -------------------------------------------------------------
const pool = ref([]);        // 未归类（有效区域为空）成员
const regions = ref([]);     // 区域 → 部门 → 成员 树
const overrides = ref([]);   // 全部覆盖行（含停用）
const loading = ref(true);
const loadError = ref('');
const busy = ref(false);     // 有接口在途：暂停拖拽与操作按钮，防止并发写入
const query = ref('');

// -- 代填授权（dim_org_proxy_grant，2026-10-21 代填人设计）----------------
// 代填人=授权替他人填数（非归属）；admin 全权，非 admin 按授权范围。
const proxyGrants = ref([]);
const proxyBusy = ref(false);
const proxyForm = ref({ proxy_user_id: '', target_region: '', target_person: '' });

//: 代填人下拉：所有在职成员（区域树各部门成员 + 未归类池，按 user_id 去重）。
const allMemberOptions = computed(() => {
  const seen = new Set();
  const list = [];
  const push = (m) => {
    if (m && m.user_id && !seen.has(m.user_id)) { seen.add(m.user_id); list.push(m); }
  };
  for (const r of regions.value) {
    for (const d of (r.depts || [])) {
      for (const m of (d.members || [])) push(m);
    }
  }
  for (const m of pool.value) push(m);
  return list;
});

//: 被代填人下拉：选中区域的成员（不选 = 整个区域）。
const proxyPersonOptions = computed(() => {
  const r = regions.value.find((x) => x.key === proxyForm.value.target_region);
  if (!r) return [];
  const list = [];
  for (const d of (r.depts || [])) for (const m of (d.members || [])) list.push(m);
  return list;
});

function memberName(userId) {
  const m = allMemberOptions.value.find((x) => x.user_id === userId);
  return (m && m.name) || userId;
}

async function loadProxyGrants() {
  if (!hasCapability('org')) { proxyGrants.value = []; return; }
  try {
    const data = await getProxyGrants();
    proxyGrants.value = (data && data.grants) || [];
  } catch (err) {
    proxyGrants.value = [];
  }
}

async function submitProxyGrant() {
  const f = proxyForm.value;
  if (!f.proxy_user_id || !f.target_region) return;
  proxyBusy.value = true;
  try {
    await grantProxy({
      proxy_user_id: f.proxy_user_id,
      target_region: f.target_region,
      target_person: f.target_person || '',
      proxy_name: memberName(f.proxy_user_id),
    });
    proxyForm.value = { proxy_user_id: '', target_region: '', target_person: '' };
    await loadProxyGrants();
    setFlash('已保存代填授权', 'ok');
  } catch (err) {
    setFlash(err.message || '保存代填授权失败', 'error');
  } finally {
    proxyBusy.value = false;
  }
}

async function toggleProxyGrant(g) {
  proxyBusy.value = true;
  try {
    await toggleProxy({
      proxy_user_id: g.proxy_user_id, target_region: g.target_region,
      target_person: g.target_person || '', enabled: !g.enabled,
    });
    await loadProxyGrants();
  } catch (err) {
    setFlash(err.message || '操作失败', 'error');
  } finally {
    proxyBusy.value = false;
  }
}

async function removeProxyGrant(g) {
  const label = `${memberName(g.proxy_user_id)} → ${g.target_region}`
    + (g.target_person ? `/${g.target_person}` : '');
  if (!window.confirm(`删除代填授权「${label}」？`)) return;
  proxyBusy.value = true;
  try {
    await deleteProxy({
      proxy_user_id: g.proxy_user_id, target_region: g.target_region,
      target_person: g.target_person || '',
    });
    await loadProxyGrants();
    setFlash('已删除代填授权', 'ok');
  } catch (err) {
    setFlash(err.message || '删除失败', 'error');
  } finally {
    proxyBusy.value = false;
  }
}

// -- 统一锚点（spec §5：region / user）+ 下游影响数 -----------------------
const anchorRegion = computed(() =>
  typeof route.query.region === 'string' ? route.query.region : '',
);
const anchorUser = computed(() =>
  typeof route.query.user === 'string' ? route.query.user : '',
);

//: 区域 key → 该区域下游定时任务数（`/api/pipelines` 分组）；null = 无权限/未加载。
const pipelineCounts = ref(null);

function pipelineCount(regionKey) {
  const map = pipelineCounts.value;
  if (!map) return null;
  return Object.prototype.hasOwnProperty.call(map, regionKey)
    ? map[regionKey]
    : 0;
}

/** 读定时任务分组，派生「区域 → 任务数」；无 pipelines 能力静默跳过。 */
async function loadPipelines() {
  if (!hasCapability('pipelines')) {
    pipelineCounts.value = null;
    return;
  }
  try {
    const data = await getPipelines();
    const map = {};
    for (const g of (data && data.groups) || []) {
      if (g && g.region) map[g.region] = g.count || 0;
    }
    pipelineCounts.value = map;
  } catch (err) {
    pipelineCounts.value = null;
  }
}

/** 落点高亮：有锚点时滚动到对应成员/区域（树加载完成后调用）。 */
async function applyAnchor() {
  if (!anchorRegion.value && !anchorUser.value) return;
  if (anchorUser.value) query.value = anchorUser.value;
  await nextTick();
  const el = document.getElementById(
    anchorUser.value ? `member-${anchorUser.value}` : `region-${anchorRegion.value}`,
  );
  if (el) el.scrollIntoView({ block: 'center' });
}

// -- 覆盖行索引 -----------------------------------------------------------
const overrideMap = computed(() => {
  const map = {};
  for (const row of overrides.value) {
    if (row && row.user_id) map[row.user_id] = row;
  }
  return map;
});

function overrideRow(userId) {
  return overrideMap.value[userId] || null;
}

function hasOverride(userId) {
  return !!overrideMap.value[userId];
}

function isEnabled(userId) {
  const row = overrideMap.value[userId];
  return !!row && Number(row.enabled) === 1;
}

function overrideStatusText(userId) {
  const row = overrideMap.value[userId];
  if (!row) return '无覆盖（种子口径）';
  return Number(row.enabled) === 1 ? '覆盖生效中' : '覆盖已停用（种子口径）';
}

// -- 搜索过滤 -------------------------------------------------------------
const activeQuery = computed(() => query.value.trim().toLowerCase());

function match(member) {
  const q = activeQuery.value;
  if (!q) return true;
  const name = String((member && member.name) || '').toLowerCase();
  const uid = String((member && member.user_id) || '').toLowerCase();
  return name.includes(q) || uid.includes(q);
}

function deptVisible(dept) {
  if (!activeQuery.value) return true;
  return (dept.members || []).some(match);
}

function regionVisible(region) {
  if (!activeQuery.value) return true;
  return (region.depts || []).some(deptVisible);
}

// 搜索状态下暂停拖拽（拖拽基于真实数组，避免过滤视图与排序相互干扰）。
const dragDisabled = computed(() => !!activeQuery.value || busy.value);

// -- 加载 -----------------------------------------------------------------
/** 给部门补一个列表内唯一 key（vuedraggable 需要 item-key）。 */
function decorate(data) {
  for (const region of data.regions || []) {
    for (const dept of region.depts || []) {
      dept._key = `${region.key}::${dept.dept_id || dept.dept_name || ''}`;
    }
  }
}

async function load({ silent = false } = {}) {
  if (!silent) loading.value = true;
  loadError.value = '';
  try {
    const data = await getOverview();
    decorate(data);
    pool.value = data.pool || [];
    regions.value = data.regions || [];
    overrides.value = data.overrides || [];
  } catch (err) {
    loadError.value = err.message;
  } finally {
    if (!silent) loading.value = false;
  }
  await applyAnchor();
}

function regionLabel(key) {
  const hit = regions.value.find((r) => r.key === key);
  return hit ? hit.label : key;
}

// 拖拽组：成员走 org-members，部门走 org-depts，两组互不串门。
const memberGroup = { name: 'org-members', pull: true, put: true };
const deptGroup = { name: 'org-depts', pull: true, put: true };

// -- 拖拽落位：成员 → 某部门 ---------------------------------------------
async function onMembersChange(evt, region, dept) {
  if (!evt.added) return; // 只有「加入」才是跨列表落位；同列表内排序不落库
  await applyAssign(evt.added.element, region.key, dept);
}

// -- 拖拽落位：成员 → 未归类池 -------------------------------------------
async function onPoolChange(evt) {
  if (!evt.added) return;
  await applyAssign(evt.added.element, null, { dept_id: null, dept_name: '' });
}

// -- 拖拽落位：整个部门 → 另一个区域 -------------------------------------
async function onDeptsChange(evt, region) {
  if (!evt.added) return;
  await applyDeptMove(evt.added.element, region);
}

/** 成员归位：乐观更新已由 vuedraggable 完成；成功后重拉，失败重拉即回滚。 */
async function applyAssign(member, regionKey, dept) {
  const who = member.name || member.user_id;
  busy.value = true;
  try {
    await assignMember({
      user_id: member.user_id,
      region: regionKey,
      dept_id: dept.dept_id || null,
      dept_name: dept.dept_name || '',
      name: member.name || null,
    });
    setFlash(
      'ok',
      regionKey
        ? `已把「${who}」归入 ${regionLabel(regionKey)}`
        : `已把「${who}」移出全部区域（未归类）`,
    );
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}（已回滚）`);
    await load({ silent: true });
  } finally {
    busy.value = false;
  }
}

/** 部门整组迁移：对该部门下每个成员依次 assign 到新区域。 */
async function applyDeptMove(dept, region) {
  const members = dept.members || [];
  const label = dept.dept_name || dept.dept_id || '未命名部门';
  if (!members.length) {
    setFlash('ok', `部门「${label}」下无成员，无需迁移`);
    await load({ silent: true });
    return;
  }
  busy.value = true;
  try {
    for (const member of members) {
      await assignMember({
        user_id: member.user_id,
        region: region.key,
        dept_id: dept.dept_id || null,
        dept_name: dept.dept_name || '',
        name: member.name || null,
      });
    }
    setFlash('ok', `已把部门「${label}」整组迁入 ${regionLabel(region.key)}（${members.length} 人）`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `部门迁移失败：${err.message}（已回滚）`);
    await load({ silent: true });
  } finally {
    busy.value = false;
  }
}

// -- 覆盖操作：停用/启用、删除、全部归还 ---------------------------------
async function onToggle(userId) {
  const next = !isEnabled(userId);
  busy.value = true;
  try {
    await toggleOverride(userId, next);
    setFlash('ok', next ? '已启用覆盖' : '已停用覆盖（回种子口径）');
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onDelete(userId) {
  busy.value = true;
  try {
    await deleteOverride(userId);
    setFlash('ok', '已删除覆盖（回种子口径）');
    detailUserId.value = null;
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onReset() {
  const total = overrides.value.length;
  if (!total) {
    setFlash('ok', '当前没有覆盖行，已是种子口径');
    return;
  }
  const ok = window.confirm(
    `将清除全部 ${total} 条覆盖，所有人回种子口径。此操作不可撤销，是否继续？`,
  );
  if (!ok) return;
  busy.value = true;
  try {
    const res = await resetOverrides();
    const deleted = res && res.deleted != null ? res.deleted : total;
    setFlash('ok', `已归还种子口径，清除 ${deleted} 条覆盖`);
    detailUserId.value = null;
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

// -- 成员详情弹层 ---------------------------------------------------------
const detailUserId = ref(null);

const detailMember = computed(() => {
  const uid = detailUserId.value;
  if (!uid) return null;
  for (const member of pool.value) {
    if (member.user_id === uid) return member;
  }
  for (const region of regions.value) {
    for (const dept of region.depts || []) {
      for (const member of dept.members || []) {
        if (member.user_id === uid) return member;
      }
    }
  }
  return null;
});

function openDetail(member) {
  detailUserId.value = member.user_id;
}

// -- 新增区域 / 部门 / 就地录月目标 抽屉 ---------------------------------
const drawer = ref(null);            // null | 'region' | 'dept' | 'target'
const drawerError = ref('');
const drawerBusy = ref(false);
const regionForm = ref({ region_key: '', label: '' });
const deptForm = ref({ region_key: '', dept_name: '', dept_id: '' });
//: 就地操作抽屉的目标区域（'target' 模式）与目标月份。
const targetScope = ref('');
const currentMonth = (() => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
})();

function openRegionDrawer() {
  regionForm.value = { region_key: '', label: '' };
  drawerError.value = '';
  drawer.value = 'region';
}

function openDeptDrawer(region) {
  deptForm.value = { region_key: region.key, dept_name: '', dept_id: '' };
  drawerError.value = '';
  drawer.value = 'dept';
}

/** 就地操作：区域头「+ 月目标」→ 右侧抽屉内嵌 TargetEditor。 */
function openTargetDrawer(region) {
  targetScope.value = region.key;
  drawerError.value = '';
  drawer.value = 'target';
}

/** TargetEditor 保存成功：原地 flash + 关抽屉 + 刷新下游影响数。 */
async function onTargetSaved() {
  const label = regionLabel(targetScope.value);
  setFlash('ok', `已保存「${label}」${currentMonth} 月目标`);
  closeDrawer();
  await Promise.all([load({ silent: true }), loadPipelines()]);
}

function closeDrawer() {
  drawer.value = null;
  drawerError.value = '';
}

async function submitRegion() {
  const region_key = regionForm.value.region_key.trim();
  const label = regionForm.value.label.trim();
  if (!region_key || !label) {
    drawerError.value = '区域键与中文名均为必填';
    return;
  }
  drawerBusy.value = true;
  drawerError.value = '';
  try {
    await addRegion({ region_key, label });
    setFlash('ok', `已登记区域「${label}」`);
    closeDrawer();
    await load({ silent: true });
  } catch (err) {
    drawerError.value = err.message;
  } finally {
    drawerBusy.value = false;
  }
}

async function submitDept() {
  const region_key = deptForm.value.region_key.trim();
  const dept_name = deptForm.value.dept_name.trim();
  const dept_id = deptForm.value.dept_id.trim();
  if (!dept_name) {
    drawerError.value = '部门名称为必填';
    return;
  }
  drawerBusy.value = true;
  drawerError.value = '';
  try {
    await addDept({ region_key, dept_name, dept_id: dept_id || null });
    setFlash('ok', `已在 ${regionLabel(region_key)} 登记部门「${dept_name}」`);
    closeDrawer();
    await load({ silent: true });
  } catch (err) {
    drawerError.value = err.message;
  } finally {
    drawerBusy.value = false;
  }
}

// -- 折叠 / 停用 / 删除 ----------------------------------------------------
const collapsed = ref(new Set());    // 折叠的区域 key 集合

function toggleCollapse(regionKey) {
  const next = new Set(collapsed.value);
  if (next.has(regionKey)) next.delete(regionKey);
  else next.add(regionKey);
  collapsed.value = next;
}

function isCollapsed(regionKey) {
  return collapsed.value.has(regionKey);
}

async function onDisableRegion(region, enabled) {
  const action = enabled ? '启用' : '停用';
  if (!enabled && !window.confirm(
    `停用区域「${region.label}」后，该区域及下挂部门将不在树中显示（可重新启用）。是否继续？`,
  )) return;
  busy.value = true;
  try {
    await disableRegion({ region_key: region.key, enabled });
    setFlash('ok', `已${action}区域「${region.label}」`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onDeleteRegion(region) {
  if (!window.confirm(
    `删除区域「${region.label}」将级联删除其下所有登记部门（已归属该区域的成员回种子口径）。此操作不可撤销，是否继续？`,
  )) return;
  busy.value = true;
  try {
    await deleteRegion({ region_key: region.key });
    setFlash('ok', `已删除区域「${region.label}」`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onDisableDept(region, dept, enabled) {
  const action = enabled ? '启用' : '停用';
  if (!enabled && dept.members.length && !window.confirm(
    `部门「${dept.dept_name}」下有 ${dept.members.length} 名成员，停用后成员将回未归类池。是否继续？`,
  )) return;
  busy.value = true;
  try {
    await disableDept({
      region_key: region.key, dept_name: dept.dept_name, enabled,
    });
    setFlash('ok', `已${action}部门「${dept.dept_name}」`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onDeleteDept(region, dept) {
  if (!window.confirm(
    `删除部门「${dept.dept_name}」后，其下成员将回未归类池。此操作不可撤销，是否继续？`,
  )) return;
  busy.value = true;
  try {
    await deleteDept({ region_key: region.key, dept_name: dept.dept_name });
    setFlash('ok', `已删除部门「${dept.dept_name}」`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  await Promise.all([load(), loadPipelines(), loadProxyGrants()]);
});
watch(() => route.query, () => applyAnchor());
</script>

<template>
  <div class="page">
    <div class="head">
      <div class="head-main">
        <h1>组织编排</h1>
        <p class="note">
          本页改的是「消费侧覆盖」，不改钉钉组织；覆盖立即影响催办与报表，回退 = 停用 / 删除覆盖行。
        </p>
      </div>
      <div class="head-actions">
        <input
          v-model="query"
          class="search"
          type="search"
          placeholder="搜索姓名 / 账号"
        />
        <button
          v-if="hasCapability('org')"
          class="btn danger"
          :disabled="busy || loading"
          @click="onReset"
        >
          全部归还种子口径
        </button>
      </div>
    </div>

    <div v-if="loading" class="state">加载中…</div>

    <div v-else-if="loadError" class="state error">
      <p>加载失败：{{ loadError }}</p>
      <button class="btn" @click="load()">重试</button>
    </div>

    <div v-else class="grid">
      <!-- 左：未归类池 -->
      <section class="col">
        <div class="col-head">
          <h2>未归类（池）</h2>
          <span class="count">{{ pool.length }}</span>
        </div>
        <p v-if="dragDisabled && activeQuery" class="hint">
          搜索中已暂停拖拽，清空搜索后可继续编排。
        </p>
        <draggable
          class="dropzone"
          :list="pool"
          :group="memberGroup"
          item-key="user_id"
          :disabled="dragDisabled"
          :animation="150"
          @change="onPoolChange"
        >
          <template #item="{ element }">
            <div
              v-show="match(element)"
              :id="'member-' + element.user_id"
              class="chip"
              :class="{ 'is-override': hasOverride(element.user_id), 'anchor-hit': anchorUser === element.user_id }"
              @click="openDetail(element)"
            >
              <span class="chip-name">{{ element.name || element.user_id }}</span>
              <span class="chip-sub">{{ element.user_id }}</span>
              <span
                v-if="hasOverride(element.user_id)"
                class="badge"
                :class="{ off: !isEnabled(element.user_id) }"
              >
                {{ isEnabled(element.user_id) ? '覆盖' : '覆盖·停用' }}
              </span>
            </div>
          </template>
        </draggable>
        <div v-if="!pool.length" class="empty">暂无未归类成员</div>
        <div v-else-if="activeQuery && !pool.some(match)" class="empty">
          无匹配的未归类成员
        </div>
      </section>

      <!-- 右：区域 → 部门 → 成员 -->
      <section class="col">
        <div class="col-head">
          <h2>区域 → 部门 → 成员</h2>
          <div class="col-head-actions">
            <span class="count">{{ regions.length }} 个区域</span>
            <button
              v-if="hasCapability('org')"
              class="btn mini"
              :disabled="busy || loading"
              title="登记一个区域（即使暂无成员也会出现在树里）"
              @click="openRegionDrawer"
            >
              + 新增区域
            </button>
          </div>
        </div>
        <div v-if="!regions.length" class="empty">暂无已归类成员</div>
        <div
          v-for="region in regions"
          v-show="regionVisible(region)"
          :id="'region-' + region.key"
          :key="region.key"
          class="region-card"
          :class="{ 'anchor-hit': anchorRegion === region.key }"
        >
          <div class="region-head">
            <span
              class="collapse-arrow"
              :class="{ collapsed: isCollapsed(region.key) }"
              @click="toggleCollapse(region.key)"
            >▶</span>
            <h3 @click="toggleCollapse(region.key)" class="region-title">
              {{ region.label }}
            </h3>
            <span class="region-key">{{ region.key }}</span>
            <div class="region-actions">
              <router-link
                v-if="hasCapability('roster', region.key)"
                class="btn mini"
                :to="{ path: '/roster', query: { region: region.key } }"
                title="去名册页登记/查看该区域的填报人"
              >
                填报名册
              </router-link>
              <button
                v-if="hasCapability('roster', region.key)"
                class="btn mini primary"
                :disabled="busy || loading"
                title="就地录入该区域当月个人月目标（万元）"
                @click="openTargetDrawer(region)"
              >
                + 月目标
              </button>
              <router-link
                v-if="hasCapability('pipelines') && pipelineCount(region.key) > 0"
                class="down-link"
                :to="{ path: '/pipelines', query: { region: region.key } }"
                :title="`该区域下游定时任务（点开定时任务页并按区域高亮）`"
              >
                下游 {{ pipelineCount(region.key) }} 项
              </router-link>
              <!-- 0 任务区域无对应分组（#group-<key> 不存在），跳过去会静默失效，
                   故只显示不可点的灰字，不给死链。 -->
              <span
                v-else-if="hasCapability('pipelines') && pipelineCount(region.key) === 0"
                class="down-link is-zero"
                title="该区域暂无下游定时任务"
              >
                下游 0 项
              </span>
              <button
                v-if="hasCapability('org')"
                class="btn mini"
                :disabled="busy || loading"
                :title="`在 ${region.label} 登记一个部门（即使暂无成员也会出现在树里）`"
                @click="openDeptDrawer(region)"
              >
                + 部门
              </button>
              <button
                v-if="hasCapability('org')"
                class="btn mini ghost"
                :disabled="busy || loading"
                title="停用该区域（可重新启用）"
                @click="onDisableRegion(region, false)"
              >
                停用
              </button>
              <button
                v-if="hasCapability('org')"
                class="btn mini danger"
                :disabled="busy || loading"
                title="删除该区域（级联删除其下部门）"
                @click="onDeleteRegion(region)"
              >
                删除
              </button>
            </div>
          </div>
          <draggable
            v-show="!isCollapsed(region.key)"
            class="dept-drop"
            :list="region.depts"
            :group="deptGroup"
            item-key="_key"
            handle=".dept-head"
            :disabled="dragDisabled"
            :animation="150"
            @change="onDeptsChange($event, region)"
          >
            <template #item="{ element: dept }">
              <div v-show="deptVisible(dept)" class="dept">
                <div class="dept-head" title="拖拽部门头部可整组迁入其他区域">
                  <span class="dept-name">{{ dept.dept_name || '未命名部门' }}</span>
                  <span class="dept-id">{{ dept.dept_id || '无ID' }}</span>
                  <span class="dept-count">{{ dept.members.length }} 人</span>
                  <button
                    v-if="hasCapability('org')"
                    class="btn mini ghost"
                    :disabled="busy || loading"
                    title="停用该部门（可重新启用）"
                    @click.stop="onDisableDept(region, dept, false)"
                  >
                    停用
                  </button>
                  <button
                    v-if="hasCapability('org')"
                    class="btn mini danger"
                    :disabled="busy || loading"
                    title="删除该部门"
                    @click.stop="onDeleteDept(region, dept)"
                  >
                    删除
                  </button>
                </div>
                <draggable
                  class="members-drop"
                  :list="dept.members"
                  :group="memberGroup"
                  item-key="user_id"
                  :disabled="dragDisabled"
                  :animation="150"
                  @change="onMembersChange($event, region, dept)"
                >
                  <template #item="{ element: member }">
                    <div
                      v-show="match(member)"
                      :id="'member-' + member.user_id"
                      class="chip"
                      :class="{ 'is-override': hasOverride(member.user_id), 'anchor-hit': anchorUser === member.user_id }"
                      @click="openDetail(member)"
                    >
                      <span class="chip-name">{{ member.name || member.user_id }}</span>
                      <span class="chip-sub">{{ member.user_id }}</span>
                      <span
                        v-if="hasOverride(member.user_id)"
                        class="badge"
                        :class="{ off: !isEnabled(member.user_id) }"
                      >
                        {{ isEnabled(member.user_id) ? '覆盖' : '覆盖·停用' }}
                      </span>
                    </div>
                  </template>
                </draggable>
              </div>
            </template>
          </draggable>
        </div>
      </section>
    </div>

    <!-- 代填授权（dim_org_proxy_grant，2026-10-21 代填人设计） -->
    <section v-if="hasCapability('org')" class="col proxy-section">
      <div class="col-head">
        <h2>代填授权</h2>
        <span class="count">{{ proxyGrants.length }}</span>
      </div>
      <p class="note">
        代填人被授权替他人填销售日报（如内勤代填某区域）。admin 可代填所有；
        这里给非 admin 的指定代填人授权范围。代填数据记在被代填人名下、留痕代填人。
      </p>

      <div class="proxy-form">
        <select v-model="proxyForm.proxy_user_id" class="input" :disabled="proxyBusy">
          <option value="" disabled>代填人（选成员）</option>
          <option v-for="m in allMemberOptions" :key="m.user_id" :value="m.user_id">
            {{ m.name || m.user_id }}
          </option>
        </select>
        <select v-model="proxyForm.target_region" class="input" :disabled="proxyBusy">
          <option value="" disabled>被代填区域</option>
          <option v-for="r in regions" :key="r.key" :value="r.key">{{ r.label }}</option>
        </select>
        <select
          v-model="proxyForm.target_person"
          class="input"
          :disabled="proxyBusy || !proxyForm.target_region"
        >
          <option value="">（整个区域）</option>
          <option v-for="m in proxyPersonOptions" :key="m.user_id" :value="m.name">
            {{ m.name || m.user_id }}
          </option>
        </select>
        <button
          class="btn primary"
          :disabled="proxyBusy || !proxyForm.proxy_user_id || !proxyForm.target_region"
          @click="submitProxyGrant"
        >
          + 授权
        </button>
      </div>

      <div v-if="!proxyGrants.length" class="empty">暂无代填授权</div>
      <table v-else class="proxy-table">
        <thead>
          <tr><th>代填人</th><th>被代填区域</th><th>被代填人</th><th>状态</th><th /></tr>
        </thead>
        <tbody>
          <tr v-for="g in proxyGrants" :key="g.id">
            <td>{{ g.proxy_name || g.proxy_user_id }}</td>
            <td>{{ regionLabel(g.target_region) }}</td>
            <td>{{ g.target_person || '（整个区域）' }}</td>
            <td>
              <span class="badge" :class="{ off: !g.enabled }">
                {{ g.enabled ? '启用' : '停用' }}
              </span>
            </td>
            <td class="ops">
              <button class="btn mini ghost" :disabled="proxyBusy" @click="toggleProxyGrant(g)">
                {{ g.enabled ? '停用' : '启用' }}
              </button>
              <button class="btn mini danger" :disabled="proxyBusy" @click="removeProxyGrant(g)">
                删除
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 成员详情 / 覆盖操作 -->
    <div v-if="detailMember" class="modal-mask" @click.self="detailUserId = null">
      <div class="modal">
        <h3 class="modal-title">
          {{ detailMember.name || detailMember.user_id }}
        </h3>
        <dl class="modal-body">
          <div><dt>账号</dt><dd>{{ detailMember.user_id }}</dd></div>
          <div>
            <dt>归属</dt>
            <dd>{{ detailMember.region ? regionLabel(detailMember.region) : '未归类' }}</dd>
          </div>
          <div><dt>部门</dt><dd>{{ detailMember.dept_name || '—' }}</dd></div>
          <div><dt>覆盖</dt><dd>{{ overrideStatusText(detailMember.user_id) }}</dd></div>
          <template v-if="overrideRow(detailMember.user_id)">
            <div>
              <dt>更新人</dt>
              <dd>{{ overrideRow(detailMember.user_id).updated_by || '—' }}</dd>
            </div>
            <div>
              <dt>更新时间</dt>
              <dd>{{ overrideRow(detailMember.user_id).updated_at || '—' }}</dd>
            </div>
            <div>
              <dt>备注</dt>
              <dd>{{ overrideRow(detailMember.user_id).note || '—' }}</dd>
            </div>
          </template>
        </dl>
        <div class="modal-actions">
          <template v-if="overrideRow(detailMember.user_id)">
            <button class="btn" :disabled="busy" @click="onToggle(detailMember.user_id)">
              {{ isEnabled(detailMember.user_id) ? '停用覆盖' : '启用覆盖' }}
            </button>
            <button
              class="btn danger"
              :disabled="busy"
              @click="onDelete(detailMember.user_id)"
            >
              删除覆盖
            </button>
          </template>
          <span v-else class="muted">该成员无覆盖行，当前即种子口径。</span>
          <button class="btn ghost" @click="detailUserId = null">关闭</button>
        </div>
      </div>
    </div>

    <!-- 就地操作抽屉：录该区域当月个人月目标（内嵌 TargetEditor） -->
    <div v-if="drawer === 'target'" class="drawer-mask" @click.self="closeDrawer">
      <aside class="drawer">
        <h3 class="drawer-title">
          录入「{{ regionLabel(targetScope) }}」{{ currentMonth }} 月目标
        </h3>
        <p class="drawer-note">
          就地操作：录入该区域个人月目标（万元），保存即落
          <code>dim_report_target</code> 并逐条记审计；提交后可在定时任务页按区域高亮查看下游影响。
        </p>
        <TargetEditor
          :scope="targetScope"
          :month="currentMonth"
          :label="regionLabel(targetScope)"
          @saved="onTargetSaved"
        />
        <div class="drawer-actions">
          <button class="btn ghost" @click="closeDrawer">关闭</button>
        </div>
      </aside>
    </div>

    <!-- 新增区域 / 部门抽屉（右侧滑入） -->
    <div v-if="drawer === 'region' || drawer === 'dept'" class="drawer-mask" @click.self="closeDrawer">
      <aside class="drawer">
        <h3 class="drawer-title">
          {{ drawer === 'region' ? '新增区域' : '新增部门' }}
        </h3>
        <p class="drawer-note">
          {{
            drawer === 'region'
              ? '登记进组织编排树；即使暂无成员也会显示，可把成员拖入该区域。'
              : `登记进「${regionLabel(deptForm.region_key)}」；即使暂无成员也会显示，可把成员拖入该部门。`
          }}
        </p>

        <div v-if="drawer === 'region'" class="drawer-form">
          <label class="field">
            <span class="field-label">区域键（英文小写，如 hangzhou）</span>
            <input v-model="regionForm.region_key" class="input" placeholder="hangzhou" />
          </label>
          <label class="field">
            <span class="field-label">中文名</span>
            <input v-model="regionForm.label" class="input" placeholder="杭州" />
          </label>
        </div>

        <div v-else class="drawer-form">
          <label class="field">
            <span class="field-label">部门名称</span>
            <input v-model="deptForm.dept_name" class="input" placeholder="杭州销售部" />
          </label>
          <label class="field">
            <span class="field-label">部门 ID（可选，留空按部门名归属）</span>
            <input v-model="deptForm.dept_id" class="input" placeholder="1050062512" />
          </label>
        </div>

        <p v-if="drawerError" class="drawer-error">{{ drawerError }}</p>

        <div class="drawer-actions">
          <button class="btn ghost" :disabled="drawerBusy" @click="closeDrawer">
            取消
          </button>
          <button
            class="btn primary"
            :disabled="drawerBusy"
            @click="drawer === 'region' ? submitRegion() : submitDept()"
          >
            {{ drawerBusy ? '提交中…' : '确定' }}
          </button>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.page {
  max-width: 1280px;
  margin: 0 auto;
  padding: 20px;
  color: #1f2933;
  font-family: system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  font-size: 14px;
}

/* 顶部 */
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
  max-width: 720px;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.5;
}
.head-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.search {
  min-width: 200px;
  padding: 8px 10px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  font-size: 14px;
  color: #1f2933;
}

/* 按钮 */
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
.btn.danger {
  border-color: #f0a5a5;
  color: #b91c1c;
}
.btn.ghost {
  border-color: transparent;
  color: #4b5563;
}
.btn.primary {
  border-color: #2563eb;
  background: #2563eb;
  color: #fff;
}
.btn.primary:hover {
  background: #1d4ed8;
}
.btn.mini {
  padding: 3px 8px;
  font-size: 12px;
}
.col-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* flash */
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

/* 状态位 */
.state {
  padding: 48px;
  text-align: center;
  color: #6b7280;
}
.state.error {
  color: #b91c1c;
}

/* 两栏 */
.grid {
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: 16px;
  margin-top: 16px;
  align-items: start;
}
@media (max-width: 860px) {
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
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 8px;
}
.col-head h2 {
  margin: 0;
  font-size: 15px;
}
.count {
  color: #9ca3af;
  font-size: 12px;
}
.hint {
  margin: 0 0 8px;
  padding: 6px 8px;
  border-radius: 5px;
  background: #fffbeb;
  color: #b45309;
  font-size: 12px;
}

/* 拖拽容器 */
.dropzone {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 120px;
  padding: 8px;
  border: 1px dashed #d1d5db;
  border-radius: 8px;
  background: #f9fafb;
}

/* 成员 chip */
.chip {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #fff;
  font-size: 13px;
  cursor: grab;
}
.chip:hover {
  border-color: #93c5fd;
  background: #f0f7ff;
}
.chip.is-override {
  border-color: #c7d2fe;
}
.chip-name {
  font-weight: 600;
}
.chip-sub {
  color: #9ca3af;
  font-size: 11px;
}
.badge {
  margin-left: auto;
  padding: 1px 6px;
  border-radius: 999px;
  background: #e0e7ff;
  color: #3730a3;
  font-size: 10px;
  white-space: nowrap;
}
.badge.off {
  background: #f3f4f6;
  color: #6b7280;
}

/* 区域卡片 / 部门 */
.region-card {
  margin-bottom: 10px;
  padding: 10px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fcfcfd;
}
.region-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
}
.region-head h3 {
  margin: 0;
  font-size: 14px;
}
.region-key {
  color: #9ca3af;
  font-size: 11px;
}
.region-title {
  cursor: pointer;
  user-select: none;
}
.region-title:hover {
  color: #2563eb;
}
.collapse-arrow {
  display: inline-block;
  width: 14px;
  text-align: center;
  color: #9ca3af;
  cursor: pointer;
  transform: rotate(90deg);
  transition: transform 0.15s ease;
  user-select: none;
}
.collapse-arrow.collapsed {
  transform: rotate(0deg);
}
.region-actions {
  margin-left: auto;
  display: flex;
  gap: 4px;
  align-items: center;
}
.dept-drop {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 32px;
}
.dept {
  padding: 8px;
  border: 1px solid #eef0f3;
  border-radius: 7px;
  background: #fff;
}
.dept-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  cursor: grab;
}
.dept-name {
  font-weight: 600;
  font-size: 13px;
}
.dept-id {
  color: #9ca3af;
  font-size: 11px;
}
.dept-count {
  margin-left: auto;
  color: #9ca3af;
  font-size: 11px;
}
.members-drop {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 34px;
  padding: 6px;
  border: 1px dashed #e5e7eb;
  border-radius: 6px;
  background: #fafafa;
}

/* 空态 */
.empty {
  padding: 14px;
  text-align: center;
  color: #9ca3af;
  font-size: 13px;
}
.muted {
  color: #9ca3af;
  font-size: 12px;
}

/* 详情弹层 */
.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(17, 24, 39, 0.35);
}
.modal {
  width: min(420px, 92vw);
  padding: 16px;
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.15);
}
.modal-title {
  margin: 0 0 10px;
  font-size: 16px;
}
.modal-body {
  display: grid;
  gap: 5px;
  margin: 0 0 14px;
}
.modal-body > div {
  display: flex;
  gap: 10px;
  font-size: 13px;
}
.modal-body dt {
  width: 64px;
  flex: none;
  color: #6b7280;
}
.modal-body dd {
  margin: 0;
  word-break: break-all;
}
.modal-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: flex-end;
}
.modal-actions .muted {
  margin-right: auto;
}

/* 新增区域 / 部门抽屉 */
.drawer-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  justify-content: flex-end;
  background: rgba(17, 24, 39, 0.35);
}
.drawer {
  width: min(380px, 92vw);
  height: 100%;
  padding: 18px;
  background: #fff;
  box-shadow: -8px 0 24px rgba(0, 0, 0, 0.15);
  overflow-y: auto;
}
.drawer-title {
  margin: 0 0 8px;
  font-size: 16px;
}
.drawer-note {
  margin: 0 0 16px;
  color: #6b7280;
  font-size: 12px;
  line-height: 1.5;
}
.drawer-form {
  display: grid;
  gap: 12px;
}
.field {
  display: grid;
  gap: 5px;
}
.field-label {
  color: #6b7280;
  font-size: 12px;
}
.input {
  padding: 8px 10px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  font-size: 14px;
  color: #1f2933;
}
.drawer-error {
  margin: 12px 0 0;
  color: #b91c1c;
  font-size: 12px;
}
.drawer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 20px;
}
.drawer code {
  background: #f0f2f5;
  padding: 1px 5px;
  border-radius: 4px;
}

/* 统一锚点落点高亮（?region= / ?user=） */
.region-card.anchor-hit,
.chip.anchor-hit {
  border-color: #2563eb;
  box-shadow: 0 0 0 2px #bfdbfe;
}
.down-link {
  color: #2563eb;
  font-size: 12px;
  text-decoration: none;
  white-space: nowrap;
}
.down-link:hover {
  text-decoration: underline;
}
.down-link.is-zero {
  color: #9ca3af;
  cursor: default;
}
.down-link.is-zero:hover {
  text-decoration: none;
}

/* 代填授权 */
.proxy-section {
  margin-top: 16px;
}
.proxy-form {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 10px 0;
}
.proxy-form .input {
  padding: 6px 8px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  min-width: 160px;
  background: #fff;
}
.proxy-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  margin-top: 6px;
}
.proxy-table th,
.proxy-table td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid #eef1f4;
}
.proxy-table .ops {
  white-space: nowrap;
}
.proxy-table .ops .btn {
  margin-right: 6px;
}
</style>
