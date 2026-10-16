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
  addGrant,
  addRecipientOverride,
  addRosterEntry,
  assignMember,
  deleteGrant,
  deleteRecipientOverride,
  deleteRosterEntry,
  getCalendar,
  getGrants,
  getOverview,
  getPipelines,
  getRecipientOverride,
  getRecords,
  getRoster,
  resetCalendarMonth,
  runPipelineOnce,
  saveRegionTargets,
  toggleCalendarDay,
  togglePipeline,
  toggleRecipientOverride,
  toggleRosterEntry,
  updatePipelineTimes,
} from '../api.js';
import { hasCapability, isAdmin, me, setFlash, flash } from '../store/me.js';

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

// -- 布局常量 ------------------------------------------------------------------
const POOL_W = 220;
const POOL_ROW_H = 30;
const POOL_HEAD_H = 44;
const REGION_W = 300;
const REGION_H = 168;
const TASK_W = 330;
const TASK_GAP = 14;
const REGION_X = 330;
const TASK_X = 850;
const TOP_Y = 40;

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

// -- 拉线（成员池 → 区域 ＝ 归属变更）------------------------------------------
const wireDraft = ref(null);   // {member, x1, y1, x2, y2}
const poolNode = ref({ id: 'pool', x: 40, y: TOP_Y });

function memberPortY(index) {
  return poolNode.value.y + POOL_HEAD_H + index * POOL_ROW_H + POOL_ROW_H / 2;
}

function startWire(evt, member, index) {
  if (evt.button !== 0 || busy.value) return;
  if (!canPipelines.value && !canRoster.value) return;
  evt.stopPropagation();
  evt.preventDefault();
  const x1 = poolNode.value.x + POOL_W;
  const y1 = memberPortY(index);
  const canvasEl = evt.target.closest('.canvas');
  const rect = canvasEl.getBoundingClientRect();
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
    const port = hit && hit.closest
      ? hit.closest('[data-region-in]') : null;
    if (!port) return;
    dropWireOnRegion(draft.member, port.getAttribute('data-region-in'));
  }
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}

async function dropWireOnRegion(member, regionKey) {
  const region = regionNodes.value.find((n) => n.key === regionKey);
  if (!region) return;
  const ok = window.confirm(
    `将「${member.name || member.user_id}」归入 ${region.label}？`,
  );
  if (!ok) return;
  busy.value = true;
  try {
    await assignMember({
      user_id: member.user_id,
      region: regionKey,
      dept_id: member.dept_id || null,
      dept_name: member.dept_name || '',
      name: member.name || null,
    });
    setFlash('ok', `已把「${member.name || member.user_id}」归入 ${region.label}`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `归属失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

const wireDraftPath = computed(() => {
  const w = wireDraft.value;
  if (!w) return '';
  const dx = Math.max(60, Math.abs(w.x2 - w.x1) / 2);
  return `M ${w.x1} ${w.y1} C ${w.x1 + dx} ${w.y1}, ${w.x2 - dx} ${w.y2}, ${w.x2} ${w.y2}`;
});

// -- 抽屉 ----------------------------------------------------------------------
const drawer = ref(null);      // {kind: 'task'|'region', node}
const drawerTimes = ref([]);   // 任务抽屉：播报时间编辑 [{hour, minute}]

function openTaskDrawer(node) {
  drawer.value = { kind: 'task', node };
  const zh = node.service.schedule_zh || '';
  // 从 schedule_zh（每天 HH:MM / 工作日 HH:MM）解析出时间点列表；解析不出给空
  const times = [];
  for (const m of zh.matchAll(/(\d{1,2}):(\d{2})/g)) {
    times.push({ hour: Number(m[1]), minute: Number(m[2]) });
  }
  drawerTimes.value = times.length ? times : [{ hour: 8, minute: 30 }];
  loadRcpt(node.service.service_id);
}

// -- 区域抽屉（P2：名册+月目标编辑收进画布）------------------------------------
const drawerRoster = ref([]);      // 该区域名册行（可编辑副本）
const drawerTargets = ref({});     // {person_name: 目标万元}
const drawerNewName = ref('');
const drawerNewRole = ref('owner');

function openRegionDrawer(node) {
  drawer.value = { kind: 'region', node };
  const roster = (lastRaw.value && lastRaw.value.roster) || {};
  drawerRoster.value = ((roster.roster) || [])
    .filter((row) => row.scope === node.key)
    .map((row) => ({ ...row }));
  drawerTargets.value = { ...((roster.targets || {})[node.key] || {}) };
  drawerNewName.value = '';
  drawerNewRole.value = 'owner';
}

async function onToggleRosterRow(row) {
  busy.value = true;
  try {
    await toggleRosterEntry({ id: row.id, enabled: !Number(row.enabled) });
    setFlash('ok', `已${Number(row.enabled) ? '停用' : '启用'}「${row.person_name}」`);
    await load({ silent: true });
    openRegionDrawer(drawer.value.node);   // 抽屉随新数据刷新
  } catch (err) {
    setFlash('err', `名册操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onDeleteRosterRow(row) {
  if (!window.confirm(`确认从名册删除「${row.person_name}」？（留审计）`)) return;
  busy.value = true;
  try {
    await deleteRosterEntry({ id: row.id });
    setFlash('ok', `已删除名册项「${row.person_name}」`);
    await load({ silent: true });
    openRegionDrawer(drawer.value.node);
  } catch (err) {
    setFlash('err', `删除失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onAddRosterRow() {
  const node = drawer.value && drawer.value.node;
  const name = drawerNewName.value.trim();
  if (!node || !name) {
    setFlash('err', '请填姓名');
    return;
  }
  busy.value = true;
  try {
    await addRosterEntry({
      scope: node.key,
      entity_type: 'person',
      entity_key: node.label,
      person_name: name,
      role: drawerNewRole.value,
      note: '',
    });
    setFlash('ok', `已把「${name}」补录到 ${node.label} 名册`);
    drawerNewName.value = '';
    await load({ silent: true });
    openRegionDrawer(node);
  } catch (err) {
    setFlash('err', `补录失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onSaveTargets() {
  const node = drawer.value && drawer.value.node;
  if (!node) return;
  const rows = Object.entries(drawerTargets.value)
    .map(([person_name, value]) => ({
      person_name,
      monthly_target: Number(value) || 0,
    }));
  busy.value = true;
  try {
    await saveRegionTargets({ scope: node.key, rows });
    setFlash('ok', `已保存 ${node.label} 月目标（${rows.length} 人）`);
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `保存目标失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

// -- 全局节点（P3：播报日历 / 操作记录 上画布）--------------------------------
const GLOBAL_W = 220;
const GLOBAL_H = 64;
const globalNodes = ref([
  { id: 'global:calendar', label: '播报日历', desc: '点日期改播报/休息', x: 40, y: 400 },
  { id: 'global:records', label: '操作记录', desc: '六源审计流水', x: 40, y: 480 },
  { id: 'global:grants', label: '授权', desc: '能力×区域授权', x: 40, y: 560, admin: true },
]);

// -- 授权抽屉（P4，admin 专属）-------------------------------------------------
const GRANT_CAPS = [
  { key: 'roster', label: '名册与目标' },
  { key: 'org', label: '组织编排' },
  { key: 'pipelines', label: '定时任务' },
  { key: 'audit', label: '操作记录' },
  { key: 'calendar', label: '播报日历' },
];
const grantRows = ref([]);
const grantForm = ref({
  user_id: '', grant_type: 'scope', cap: 'roster', region: '', note: '',
});

async function loadGrants() {
  busy.value = true;
  try {
    const data = await getGrants();
    grantRows.value = (data && data.grants) || [];
  } catch (err) {
    setFlash('err', `授权读取失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

function grantKeyOf(f) {
  if (f.grant_type === 'admin') return '-';
  if (f.grant_type === 'region') return f.region;
  return f.region ? `${f.cap}:${f.region}` : f.cap;
}

async function onGrantAdd() {
  const f = grantForm.value;
  if (!f.user_id.trim()) {
    setFlash('err', '请填钉钉 userid');
    return;
  }
  if (f.grant_type !== 'admin' && !grantKeyOf(f)) {
    setFlash('err', '请选择能力/区域');
    return;
  }
  busy.value = true;
  try {
    await addGrant({
      user_id: f.user_id.trim(),
      grant_type: f.grant_type,
      grant_key: grantKeyOf(f),
      note: f.note.trim(),
    });
    setFlash('ok', '授权已添加（生效有分钟级延迟）');
    grantForm.value.user_id = '';
    grantForm.value.note = '';
    await loadGrants();
  } catch (err) {
    setFlash('err', `添加失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onGrantDelete(row) {
  if (!window.confirm(
    `删除授权？\n${row.user_id} · ${row.grant_type_label} · ${row.grant_key}`,
  )) return;
  busy.value = true;
  try {
    await deleteGrant({
      user_id: row.user_id,
      grant_type: row.grant_type,
      grant_key: row.grant_key,
    });
    setFlash('ok', '已删除授权');
    await loadGrants();
  } catch (err) {
    setFlash('err', `删除失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

function openGrantsDrawer() {
  drawer.value = { kind: 'grants' };
  loadGrants();
}

// -- 日历抽屉 -------------------------------------------------------------------
const calData = ref(null);
const calMonth = ref('');

async function loadCalendar(month) {
  busy.value = true;
  try {
    calData.value = await getCalendar(month);
    calMonth.value = calData.value.month;
  } catch (err) {
    setFlash('err', `日历读取失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

function openCalendarDrawer() {
  drawer.value = { kind: 'calendar' };
  loadCalendar(calMonth.value || undefined);
}

const calCells = computed(() => {
  if (!calData.value) return [];
  const cells = [];
  for (let i = 0; i < (calData.value.first_weekday || 0); i += 1) cells.push(null);
  for (const day of calData.value.days || []) cells.push(day);
  return cells;
});

async function onToggleCalDay(cell) {
  if (!cell) return;
  const date = `${calData.value.month}-${String(cell.day).padStart(2, '0')}`;
  const willWork = !cell.is_workday;
  busy.value = true;
  try {
    await toggleCalendarDay({ date, is_workday: willWork, note: '' });
    setFlash('ok', `${date} 已改为${willWork ? '播报' : '休息'}`);
    await loadCalendar(calMonth.value);
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onResetCalMonth() {
  if (!window.confirm(`清除 ${calMonth.value} 的人工裁决，回到全播报基线？`)) return;
  busy.value = true;
  try {
    await resetCalendarMonth({ month: calMonth.value });
    setFlash('ok', `${calMonth.value} 已恢复全播报基线`);
    await loadCalendar(calMonth.value);
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

// -- 记录抽屉 -------------------------------------------------------------------
const recRows = ref([]);
const recSources = ref([]);
const recFilter = ref({ source: '', actor: '' });

async function loadRecords() {
  busy.value = true;
  try {
    const data = await getRecords({
      source: recFilter.value.source || undefined,
      actor: recFilter.value.actor || undefined,
    });
    recRows.value = (data && data.records) || [];
    recSources.value = (data && data.sources) || [];
  } catch (err) {
    setFlash('err', `记录读取失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

function openRecordsDrawer() {
  drawer.value = { kind: 'records' };
  loadRecords();
}

function onGlobalNodeClick(node) {
  if (node.id === 'global:calendar') openCalendarDrawer();
  else if (node.id === 'global:records') openRecordsDrawer();
  else if (node.id === 'global:grants') openGrantsDrawer();
}

const visibleGlobalNodes = computed(() =>
  globalNodes.value.filter((n) => !n.admin || isAdmin()));

// -- 任务抽屉收件人段（P3：吃收件人总览页的单线微调）------------------------------
const RCPT_MODE = { include: '追加', exclude: '移除' };
const RCPT_TYPE = { user_id: '账号', name: '姓名' };
const rcpt = ref({ loading: false, base: [], rows: [], explain: [] });
const rcptForm = ref({ mode: 'include', target_type: 'user_id', target_value: '', note: '' });

async function loadRcpt(serviceId) {
  rcpt.value.loading = true;
  try {
    const data = await getRecipientOverride(serviceId);
    rcpt.value = {
      loading: false,
      base: (data && data.base) || [],
      rows: (data && data.overrides) || [],
      explain: (data && data.explain) || [],
    };
  } catch (err) {
    rcpt.value = { loading: false, base: [], rows: [], explain: [] };
    setFlash('err', `收件人读取失败：${err.message}`);
  }
}

async function onRcptAdd(serviceId) {
  const value = rcptForm.value.target_value.trim();
  if (!value) return;
  busy.value = true;
  try {
    await addRecipientOverride({
      service_id: serviceId,
      mode: rcptForm.value.mode,
      target_type: rcptForm.value.target_type,
      target_value: value,
      note: rcptForm.value.note.trim(),
    });
    setFlash('ok', '收件人覆盖已添加');
    rcptForm.value.target_value = '';
    rcptForm.value.note = '';
    await loadRcpt(serviceId);
  } catch (err) {
    setFlash('err', `添加失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onRcptToggle(serviceId, row) {
  busy.value = true;
  try {
    await toggleRecipientOverride({ id: row.id, enabled: !Number(row.enabled) });
    await loadRcpt(serviceId);
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

async function onRcptDelete(serviceId, row) {
  if (!window.confirm(`删除覆盖「${RCPT_MODE[row.mode]} ${row.target_value}」？`)) return;
  busy.value = true;
  try {
    await deleteRecipientOverride({ id: row.id });
    await loadRcpt(serviceId);
  } catch (err) {
    setFlash('err', `删除失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

function closeDrawer() {
  drawer.value = null;
}

function addTimeRow() {
  drawerTimes.value.push({ hour: 12, minute: 0 });
}
function removeTimeRow(index) {
  drawerTimes.value.splice(index, 1);
}

async function onSaveTimes() {
  const node = drawer.value && drawer.value.node;
  if (!node) return;
  const times = drawerTimes.value
    .filter((t) => Number.isInteger(t.hour) && Number.isInteger(t.minute));
  if (!times.length) {
    setFlash('err', '至少保留一个播报时间');
    return;
  }
  busy.value = true;
  try {
    await updatePipelineTimes({ service_id: node.service.service_id, times });
    setFlash('ok', `已改「${node.service.service_label}」播报时间`);
    closeDrawer();
    await load({ silent: true });
  } catch (err) {
    setFlash('err', `改播报时间失败：${err.message}`);
  } finally {
    busy.value = false;
  }
}

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

function taskBroken(service) {
  if (!service.enabled) return true;
  const latest = service.latest;
  return !!(latest && latest.status === 'failed');
}

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
  const regionSpecs = regions.map((region) => ({ key: region.key, region }));
  const platform = servicesByRegion.__platform__ || [];
  if (platform.length) {
    regionSpecs.push({ key: '__platform__', region: null });
  }

  for (const spec of regionSpecs) {
    const key = spec.key;
    const isPlatform = key === '__platform__';
    const memberCount = isPlatform ? 0 : (spec.region.depts || [])
      .reduce((sum, dept) => sum + (dept.members || []).length, 0);
    const rosterCount = isPlatform ? 0 : rosterRows
      .filter((row) => row.scope === key && Number(row.enabled) === 1).length;
    const targetMap = (targets[key]) || {};
    const targetCount = Object.keys(targetMap).length;
    const targetSum = Object.values(targetMap)
      .reduce((sum, value) => sum + (Number(value) || 0), 0);
    const services = servicesByRegion[key] || [];
    const badge = isPlatform
      ? { text: '平台线', cls: 'plain' }
      : regionBadge(rosterCount, targetCount, services.length);
    nextRegions.push({
      id: `region:${key}`,
      key,
      label: isPlatform ? '平台 / 无群' : (spec.region.label || key),
      badge,
      rosterCount,
      targetCount,
      targetSum,
      memberCount,
      taskCount: services.length,
      isPlatform,
      x: REGION_X,
      y: TOP_Y,
    });
    // 家族模块化（2026-10-16）：同一区域下同家族任务折叠为模块节点——
    // 家族=模板、区域=实例（robot-hangzhou/robot-shaoxing 同属 robot-*）。
    // 连线 N:1 收敛为 区域→模块；状态聚合（全绿/有停用/有失败）。
    const families = {};
    for (const service of services) {
      const family = familyOf(service.service_id, key);
      if (!families[family]) families[family] = [];
      families[family].push(service);
    }
    for (const [family, members] of Object.entries(families)) {
      const moduleId = `module:${key}:${family}`;
      const brokenCount = members.filter(taskBroken).length;
      nextTasks.push({
        id: moduleId,
        regionKey: key,
        family,
        label: familyLabel(family, members[0]),
        services: members,
        brokenCount,
        x: TASK_X,
        y: TOP_Y,
      });
      nextEdges.push({
        id: `e:${key}:${family}`,
        from: `region:${key}`,
        to: moduleId,
        broken: badge.cls === 'warn' || brokenCount > 0,
      });
    }
  }
  regionNodes.value = nextRegions;
  taskNodes.value = nextTasks;
  edges.value = nextEdges;
  layout();
}

/** 管道家族键：剥掉区域后缀（robot-check-hangzhou → robot-check）。 */
function familyOf(serviceId, regionKey) {
  const suffix = `-${regionKey}`;
  if (regionKey !== '__platform__' && serviceId.endsWith(suffix)) {
    return serviceId.slice(0, -suffix.length);
  }
  return serviceId;
}

/** 家族展示名（先映射表，缺省用首成员的类别标签）。 */
const FAMILY_LABEL = {
  'robot': '填报提醒',
  'robot-check': '催办未填 + DING',
  'pages': '榜单页生成',
  'leaderboard': '榜单群播报',
  'leaderboard-html': '榜单页生成',
  'channel-missing-check': '渠道到齐检查',
};
function familyLabel(family, firstService) {
  return FAMILY_LABEL[family]
    || `${(firstService && firstService.category_label) || '任务'}·${family}`;
}

/** 模块节点高度（折叠 64；展开按成员行数）。 */
function moduleH(node) {
  const expanded = expandedModules.value.has(node.id);
  return expanded ? 44 + node.services.length * 78 : 64;
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

function layout() {
  const saved = nodePos.value;
  let cursorY = TOP_Y;
  const poolH = POOL_HEAD_H + poolMembers.value.length * POOL_ROW_H + 12;
  const savedPool = saved.pool;
  poolNode.value = {
    id: 'pool',
    x: savedPool ? savedPool.x : 40,
    y: savedPool ? savedPool.y : TOP_Y,
  };
  // 全局节点：池下方纵向排列（可被拖动覆盖）
  const poolBottom = poolH + poolNode.value.y + 28;
  globalNodes.value.forEach((node, index) => {
    const savedPos = saved[node.id];
    node.x = savedPos ? savedPos.x : 40;
    node.y = savedPos ? savedPos.y : poolBottom + index * (GLOBAL_H + 16);
  });
  for (const region of regionNodes.value) {
    const stack = taskNodes.value.filter((t) => t.regionKey === region.key);
    const stackTop = cursorY;
    stack.forEach((task) => {
      const savedPos = saved[task.id];
      task.x = savedPos ? savedPos.x : TASK_X;
      task.y = savedPos ? savedPos.y : cursorY;
      cursorY += moduleH(task) + TASK_GAP;
    });
    const stackH = cursorY - stackTop - TASK_GAP;
    const regionY = stack.length
      ? stackTop + Math.max(0, (stackH - REGION_H) / 2)
      : cursorY;
    if (!stack.length) cursorY += REGION_H;
    cursorY += 26;
    const savedPos = saved[region.id];
    region.x = savedPos ? savedPos.x : REGION_X;
    region.y = savedPos ? savedPos.y : Math.round(regionY);
  }
  void poolH;
}

// -- 连线几何 -------------------------------------------------------------------
const nodeById = computed(() => {
  const map = {};
  for (const n of regionNodes.value) map[n.id] = { ...n, w: REGION_W, h: REGION_H };
  for (const n of taskNodes.value) map[n.id] = { ...n, w: TASK_W, h: moduleH(n) };
  return map;
});

const edgePaths = computed(() => edges.value.map((edge) => {
  const from = nodeById.value[edge.from];
  const to = nodeById.value[edge.to];
  if (!from || !to) return { ...edge, d: '' };
  const x1 = from.x + from.w;
  const y1 = from.y + from.h / 2;
  const x2 = to.x;
  const y2 = to.y + 40;
  const dx = Math.max(60, (x2 - x1) / 2);
  return {
    ...edge,
    d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`,
  };
}));

const canvasSize = computed(() => {
  let maxX = 1300;
  let maxY = 480;
  for (const n of Object.values(nodeById.value)) {
    maxX = Math.max(maxX, n.x + n.w + 60);
    maxY = Math.max(maxY, n.y + n.h + 60);
  }
  for (const n of globalNodes.value) {
    maxY = Math.max(maxY, n.y + GLOBAL_H + 60);
  }
  return { w: maxX, h: maxY };
});

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
          连线即驱动关系；从池成员端口拉线到区域＝归入该区；任务节点内嵌开关与
          「运行一次」，点节点开抽屉改播报时间。名册/日历/记录/授权陆续上画布。
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
              :class="{ broken: edge.broken }"
            />
            <path v-if="wireDraft" :d="wireDraftPath" class="edge drafting" />
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
                title="拉线到区域节点＝归入该区"
                @mousedown="startWire($event, member, index)"
              />
            </div>
            <p v-if="!poolMembers.length" class="pool-hint">全员已归口</p>
          </article>

          <!-- 区域节点 -->
          <article
            v-for="node in regionNodes"
            :key="node.id"
            :id="`flow-${node.key}`"
            class="node region-node"
            :class="{ 'anchor-hit': anchorRegion === node.key }"
            :style="{ left: node.x + 'px', top: node.y + 'px', width: REGION_W + 'px' }"
          >
            <span class="port in-region" :data-region-in="node.key" title="成员归口输入" />
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
            <div class="node-foot">
              <span class="task-count">下游任务 {{ node.taskCount }}</span>
              <button class="btn mini ghost" @click="openRegionDrawer(node)">详情</button>
            </div>
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
            <span class="port in" />
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
                  <button class="btn mini ghost" @click="openTaskDrawer({ service })">详情</button>
                </div>
              </div>
            </div>
          </article>
        </div>
      </div>

      <div class="legend">
        <span class="lg"><i class="line ok" /> 正常驱动</span>
        <span class="lg"><i class="line broken" /> 断链（缺名册/缺目标/停用/失败）</span>
        <span class="lg"><i class="line drafting" /> 拉线中＝归口</span>
        <span class="lg">滚轮缩放 · 拖空白平移 · 拖节点头移动</span>
      </div>
    </div>

    <!-- 节点抽屉 -->
    <transition name="drawer">
      <aside v-if="drawer" class="drawer">
        <template v-if="drawer.kind === 'task'">
          <h2>{{ drawer.node.service.service_label }}</h2>
          <p class="drawer-sub">{{ drawer.node.service.service_id }}</p>
          <div class="drawer-sec">
            <h3>播报时间</h3>
            <div
              v-for="(t, index) in drawerTimes"
              :key="index"
              class="time-row"
            >
              <input v-model.number="t.hour" type="number" min="0" max="23"> 时
              <input v-model.number="t.minute" type="number" min="0" max="59"> 分
              <button
                class="btn mini ghost"
                :disabled="drawerTimes.length <= 1"
                @click="removeTimeRow(index)"
              >删</button>
            </div>
            <button class="btn mini" @click="addTimeRow">+ 加一个时点</button>
            <button class="btn mini primary" :disabled="busy" @click="onSaveTimes">保存时间</button>
          </div>
          <div class="drawer-sec">
            <h3>最近运行</h3>
            <p v-if="drawer.node.service.latest" class="drawer-line">
              {{ drawer.node.service.latest.status_label }} ·
              {{ drawer.node.service.latest.created_at }} ·
              {{ drawer.node.service.latest.requested_by }}
            </p>
            <p v-else class="drawer-line muted">无运行记录</p>
            <router-link class="drawer-link" to="/pipelines/history" @click="closeDrawer">
              完整执行流水 →
            </router-link>
          </div>
          <div class="drawer-sec" v-if="drawer.node.service.depends_on.length">
            <h3>依赖</h3>
            <p class="drawer-line">{{ drawer.node.service.depends_on.join('、') }}</p>
          </div>
          <div class="drawer-sec">
            <h3>收件人覆盖</h3>
            <p class="drawer-line muted" v-if="rcpt.loading">读取中…</p>
            <template v-else>
              <p class="drawer-line muted">
                基名单 {{ rcpt.base.length }} 人（{{ rcpt.base.join('、') || '—' }}）
              </p>
              <div v-for="o in rcpt.rows" :key="o.id" class="roster-row">
                <span class="rcpt-mode" :class="o.mode">{{ RCPT_MODE[o.mode] || o.mode }}</span>
                <span class="roster-name">{{ o.target_value }}</span>
                <span class="roster-role">{{ RCPT_TYPE[o.target_type] || o.target_type }}</span>
                <button
                  class="switch"
                  :class="{ on: Number(o.enabled) }"
                  :disabled="busy"
                  @click="onRcptToggle(drawer.node.service.service_id, o)"
                ><i /></button>
                <button
                  class="btn mini ghost"
                  :disabled="busy"
                  @click="onRcptDelete(drawer.node.service.service_id, o)"
                >删</button>
              </div>
              <p v-if="!rcpt.rows.length" class="drawer-line muted">
                暂无覆盖（按名册口径发送）。
              </p>
              <div class="roster-add">
                <select v-model="rcptForm.mode" class="roster-select">
                  <option value="include">追加</option>
                  <option value="exclude">移除</option>
                </select>
                <select v-model="rcptForm.target_type" class="roster-select">
                  <option value="user_id">账号</option>
                  <option value="name">姓名</option>
                </select>
                <input v-model="rcptForm.target_value" placeholder="账号或姓名" class="roster-input">
                <button
                  class="btn mini"
                  :disabled="busy"
                  @click="onRcptAdd(drawer.node.service.service_id)"
                >添加</button>
              </div>
            </template>
          </div>
        </template>
        <template v-else-if="drawer.kind === 'region'">
          <h2>{{ drawer.node.label }}</h2>
          <p class="drawer-sub">
            {{ drawer.node.key }} · 成员 {{ drawer.node.memberCount }} ·
            下游任务 {{ drawer.node.taskCount }} · 目标月份 {{ month }}
          </p>
          <div class="drawer-sec">
            <h3>名册（{{ drawerRoster.length }}）</h3>
            <div v-for="row in drawerRoster" :key="row.id" class="roster-row">
              <span class="roster-name" :class="{ off: !Number(row.enabled) }">
                {{ row.person_name }}
              </span>
              <span class="roster-role">{{ row.role === 'deputy' ? '代填' : '负责' }}</span>
              <button
                class="switch"
                :class="{ on: Number(row.enabled) }"
                :disabled="busy"
                @click="onToggleRosterRow(row)"
              ><i /></button>
              <button
                class="btn mini ghost"
                :disabled="busy"
                @click="onDeleteRosterRow(row)"
              >删</button>
            </div>
            <div class="roster-add">
              <input v-model="drawerNewName" placeholder="姓名" class="roster-input">
              <select v-model="drawerNewRole" class="roster-select">
                <option value="owner">负责人</option>
                <option value="deputy">代填人</option>
              </select>
              <button class="btn mini" :disabled="busy" @click="onAddRosterRow">补录</button>
            </div>
          </div>
          <div class="drawer-sec">
            <h3>月目标（万元）</h3>
            <div
              v-for="row in drawerRoster.filter((r) => Number(r.enabled))"
              :key="'t' + row.id"
              class="target-row"
            >
              <span class="roster-name">{{ row.person_name }}</span>
              <input
                v-model="drawerTargets[row.person_name]"
                type="number"
                min="0"
                step="0.1"
                class="target-input"
              >
            </div>
            <button class="btn mini primary" :disabled="busy" @click="onSaveTargets">
              保存月目标
            </button>
            <p class="drawer-line muted">
              保存即写当月快照真源（dim_report_target）；发布快照/月份切换暂留名册页。
            </p>
          </div>
        </template>
        <template v-else-if="drawer.kind === 'calendar'">
          <h2>播报日历</h2>
          <p class="drawer-sub">默认每天播报；点日期=切换 播报/休息（人工裁决，留审计）</p>
          <div class="drawer-sec" v-if="calData">
            <div class="cal-nav">
              <button class="btn mini" @click="loadCalendar(calData.prev_month)">← 上月</button>
              <b>{{ calData.year }} 年 {{ calData.month_no }} 月</b>
              <button class="btn mini" @click="loadCalendar(calData.next_month)">下月 →</button>
            </div>
            <div class="cal-grid">
              <span v-for="w in ['一','二','三','四','五','六','日']" :key="w" class="cal-wd">{{ w }}</span>
              <template v-for="(cell, i) in calCells" :key="i">
                <span v-if="!cell" class="cal-cell empty" />
                <button
                  v-else
                  class="cal-cell"
                  :class="{ rest: !cell.is_workday, manual: cell.source === 'manual' }"
                  :title="`${cell.day} 日 ${cell.is_workday ? '播报' : '休息'}${cell.note ? ' · ' + cell.note : ''}`"
                  @click="onToggleCalDay(cell)"
                >{{ cell.day }}</button>
              </template>
            </div>
            <p class="drawer-line muted">
              播报日 {{ calData.days.filter((d) => d.is_workday).length }} 天 ·
              休息 {{ calData.days.filter((d) => !d.is_workday).length }} 天
            </p>
            <button class="btn mini" :disabled="busy" @click="onResetCalMonth">
              清除本月人工裁决（回全播报）
            </button>
          </div>
        </template>
        <template v-else-if="drawer.kind === 'records'">
          <h2>操作记录</h2>
          <p class="drawer-sub">六源聚合审计（授权/定时任务/名册/播报日历/收件人/定时运行）</p>
          <div class="drawer-sec">
            <div class="rec-filter">
              <select v-model="recFilter.source" class="roster-select" @change="loadRecords">
                <option value="">全部来源</option>
                <option v-for="s in recSources" :key="s" :value="s">{{ s }}</option>
              </select>
              <input
                v-model="recFilter.actor"
                placeholder="操作人"
                class="roster-input"
                @change="loadRecords"
              >
            </div>
            <div v-for="(r, i) in recRows" :key="i" class="rec-row">
              <span class="rec-time">{{ r.created_at }}</span>
              <span class="rec-source">{{ r.source }}</span>
              <span class="rec-action">{{ r.action_label }}</span>
              <span class="rec-target">{{ r.actor }} → {{ r.target }}</span>
              <span class="rec-detail">{{ r.detail }}</span>
            </div>
            <p v-if="!recRows.length" class="drawer-line muted">无记录</p>
          </div>
        </template>
        <template v-else-if="drawer.kind === 'grants'">
          <h2>授权</h2>
          <p class="drawer-sub">能力×区域授权（admin 专属；生效有分钟级延迟）</p>
          <div class="drawer-sec">
            <h3>新增授权</h3>
            <div class="roster-add">
              <input v-model="grantForm.user_id" placeholder="钉钉 userid" class="roster-input">
              <select v-model="grantForm.grant_type" class="roster-select">
                <option value="scope">能力</option>
                <option value="region">看板区域</option>
                <option value="admin">管理员</option>
              </select>
            </div>
            <div class="roster-add" v-if="grantForm.grant_type === 'scope'">
              <select v-model="grantForm.cap" class="roster-select">
                <option v-for="c in GRANT_CAPS" :key="c.key" :value="c.key">{{ c.label }}</option>
              </select>
              <select v-model="grantForm.region" class="roster-select">
                <option value="">全局</option>
                <option v-for="n in regionNodes" :key="n.key" :value="n.key">{{ n.label }}</option>
              </select>
            </div>
            <div class="roster-add" v-else-if="grantForm.grant_type === 'region'">
              <select v-model="grantForm.region" class="roster-select">
                <option value="">— 选择区域 —</option>
                <option v-for="n in regionNodes" :key="n.key" :value="n.key">{{ n.label }}</option>
              </select>
            </div>
            <div class="roster-add">
              <input v-model="grantForm.note" placeholder="备注（可空）" class="roster-input">
              <button class="btn mini primary" :disabled="busy" @click="onGrantAdd">添加</button>
            </div>
          </div>
          <div class="drawer-sec">
            <h3>授权现状（{{ grantRows.length }}）</h3>
            <div v-for="(row, i) in grantRows" :key="i" class="rec-row">
              <span class="rec-target">{{ row.user_id }}</span>
              <span class="rec-action">{{ row.grant_type_label }}</span>
              <span class="rec-source">{{ row.grant_key }}</span>
              <button class="btn mini ghost" :disabled="busy" @click="onGrantDelete(row)">删</button>
              <span class="rec-detail">{{ row.note }} · {{ row.created_at }}</span>
            </div>
            <p v-if="!grantRows.length" class="drawer-line muted">暂无授权记录</p>
          </div>
        </template>
        <button class="drawer-close" @click="closeDrawer">✕</button>
      </aside>
    </transition>
    <div v-if="drawer" class="drawer-mask" @click="closeDrawer" />
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
.edge.drafting {
  stroke: #2563eb;
  stroke-dasharray: 4 4;
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

/* 抽屉 */
.drawer {
  position: fixed;
  right: 0;
  top: 52px;
  bottom: 0;
  width: 360px;
  background: #fff;
  border-left: 1px solid #e5e7eb;
  box-shadow: -4px 0 16px rgba(16, 24, 40, 0.08);
  padding: 18px;
  z-index: 30;
  overflow-y: auto;
}
.drawer h2 {
  margin: 0 0 2px;
  font-size: 16px;
}
.drawer-sub {
  margin: 0 0 12px;
  color: #9ca3af;
  font-size: 12px;
}
.drawer-sec {
  margin-bottom: 16px;
}
.drawer-sec h3 {
  margin: 0 0 6px;
  font-size: 13px;
  color: #4b5563;
}
.drawer-line {
  margin: 0 0 6px;
  font-size: 13px;
}
.drawer-line.muted {
  color: #9ca3af;
}
.drawer-link {
  display: block;
  margin: 4px 0;
  color: #2563eb;
  font-size: 13px;
  text-decoration: none;
}
.time-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  font-size: 13px;
}
.time-row input {
  width: 56px;
  padding: 4px 6px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
}
.drawer-close {
  position: absolute;
  right: 12px;
  top: 12px;
  border: none;
  background: none;
  font-size: 16px;
  color: #9ca3af;
  cursor: pointer;
}
.roster-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 13px;
}
.roster-name {
  min-width: 72px;
  font-weight: 600;
}
.roster-name.off {
  color: #9ca3af;
  text-decoration: line-through;
}
.roster-role {
  font-size: 11px;
  color: #6b7280;
}
.roster-add {
  display: flex;
  gap: 6px;
  margin-top: 8px;
}
.roster-input {
  flex: 1;
  padding: 4px 8px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
}
.roster-select {
  padding: 4px 6px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
}
.target-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
}
.target-input {
  width: 100px;
  padding: 4px 6px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
}
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
.rcpt-mode {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 8px;
  background: #ecfdf5;
  color: #047857;
}
.rcpt-mode.exclude {
  background: #fef2f2;
  color: #b91c1c;
}
.cal-nav {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.cal-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 4px;
  margin-bottom: 8px;
}
.cal-wd {
  text-align: center;
  font-size: 11px;
  color: #9ca3af;
}
.cal-cell {
  padding: 6px 0;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #fff;
  font-size: 12px;
  cursor: pointer;
}
.cal-cell.rest {
  background: #dbe7ff;
  color: #1f3a5f;
}
.cal-cell.manual {
  border-color: #f59e0b;
}
.cal-cell.empty {
  border: none;
  background: none;
  cursor: default;
}
.rec-filter {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
}
.rec-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 5px 0;
  border-bottom: 1px solid #f3f4f6;
  font-size: 12px;
}
.rec-time {
  color: #9ca3af;
}
.rec-source {
  color: #4338ca;
}
.rec-action {
  font-weight: 600;
}
.rec-target {
  color: #4b5563;
}
.rec-detail {
  width: 100%;
  color: #6b7280;
}
.drawer-mask {
  position: fixed;
  inset: 0;
  z-index: 20;
}
.drawer-enter-active,
.drawer-leave-active {
  transition: transform 0.18s ease;
}
.drawer-enter-from,
.drawer-leave-to {
  transform: translateX(100%);
}
</style>
