<script setup>
/**
 * 定时任务（管道注册表）页 · Vue 迁移自旧 SSR /pipelines + _PAGE_JS 客户端逻辑。
 *
 * 读面 GET /api/pipelines（分组装配）；写面 toggle/update/add/run-once 与收件人
 * 覆盖 add/toggle/delete/reset。行为口径逐条照抄 app.py `_PAGE_JS`：
 *  - 改播报时间：≤3 个 HH:MM 时点，保留原 cron 日/月/周尾段，前端合成多段 cron；
 *  - 新增：预设 + 时点合成 cron 的校验语义（自定义留空给硬提示）；
 *  - 收件人抽屉：?service=<id> 深链自动打开；reminder_html 内联的「收件人…」
 *    入口（服务端生成的 onclick=openRecipientOverride(...)）经全局桥接生效。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import {
  getPipelines,
  togglePipeline,
  archivePipeline,
  updatePipelineTimes,
  updatePipelineParams,
  addPipeline,
  runPipelineOnce,
  getRecipientOverride,
  addRecipientOverride,
  toggleRecipientOverride,
  deleteRecipientOverride,
  resetRecipientOverride,
} from '../api.js';
import { setFlash } from '../store/me.js';

const route = useRoute();

//: 统一锚点（spec §5）：?region=<key> → 高亮并滚动到该区域分组。
const anchorRegion = computed(() =>
  typeof route.query.region === 'string' ? route.query.region : '',
);

async function applyRegionAnchor() {
  if (!anchorRegion.value) return;
  await nextTick();
  const el = document.getElementById('group-' + anchorRegion.value);
  if (el) {
    if ('open' in el) el.open = true;
    el.scrollIntoView({ block: 'start' });
  }
}

/* ------------------------------ 读面 --------------------------------- */
const loading = ref(true);
const loadError = ref('');
const groups = ref([]);
const supportedCategories = ref([]);

async function load(initial = true) {
  if (initial) loading.value = true;
  loadError.value = '';
  try {
    const data = await getPipelines();
    groups.value = (data && data.groups) || [];
    supportedCategories.value = (data && data.supported_categories) || [];
  } catch (err) {
    loadError.value = err.message || '读取失败，请稍后重试';
    if (initial) groups.value = [];
  } finally {
    loading.value = false;
  }
}

/* ---------------------------- 行操作 --------------------------------- */
async function toggleTask(s) {
  const enable = !s.enabled;
  let reason = '';
  let reviewAfter = '';
  if (!enable) {
    // 治理门禁（2026-10-17）：停用必须带原因，可选复查日
    reason = (window.prompt(
      '停用 ' + s.service_id + ' 的原因？（必填，进审计）',
      s.disabled_reason || '',
    ) || '').trim();
    if (!reason) return;
    reviewAfter = (window.prompt(
      '预计复查日（YYYY-MM-DD，可空）', s.review_after || '',
    ) || '').trim();
  }
  if (!window.confirm(
    '确认' + (enable ? '启用' : '停用') + ' ' + s.service_id +
    '？（下一个调度 tick 生效，≤30s）')) return;
  try {
    await togglePipeline({
      service_id: s.service_id, enabled: enable,
      reason, review_after: reviewAfter,
    });
    setFlash('ok', (enable ? '已启用 ' : '已停用 ') + s.service_id + '（≤30s 生效）');
    await load(false);
  } catch (err) {
    setFlash('err', err.message);
  }
}

async function runOnce(s) {
  if (!window.confirm(
    '确认立即运行一次 ' + s.service_id +
    '？（调度器下个 tick 认领触发；提醒/催办/汇总类当日已发的会绕开去重重发）')) return;
  try {
    await runPipelineOnce({ service_id: s.service_id });
    setFlash('ok', '已提交立即运行 ' + s.service_id);
    await load(false);
  } catch (err) {
    setFlash('err', err.message);
  }
}

/* ---------------------------- 归档（2026-10-19 治理 P2）--------------- */
async function archiveTask(s) {
  if (!window.confirm(
    '确认归档 ' + s.service_id +
    '？归档 = 从注册表摘除该条目（审计留痕，git 历史即档案），'
    + '服务将从舰队消失、不可再调度。此操作不可在前端撤销。')) return;
  try {
    const resp = await archivePipeline({ service_id: s.service_id });
    let msg = '已归档 ' + s.service_id;
    if (resp && resp.seed_registered) {
      msg += '（注意：seed 仍有条目，需代码 PR 同步删除防复活）';
    }
    setFlash('ok', msg);
    await load(false);
  } catch (err) {
    setFlash('err', err.message);
  }
}

/* -------------------- 改播报时间（≤3 个 HH:MM，多段 cron 并集）-------- */
const CRON_DOW = { 0: '日', 1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 7: '日' };

function cronSegments(cron) {
  return String(cron || '').split(';').map((s) => s.trim()).filter((s) => s.length > 0);
}

function parseBroadcastTimes(cron) {
  const segs = cronSegments(cron);
  if (segs.length < 1 || segs.length > 3) return null;
  const times = [];
  let tail = null;
  for (const seg of segs) {
    const parts = seg.split(/\s+/);
    if (parts.length !== 5) return null;
    if (!/^\d+$/.test(parts[0])) return null;
    const mm = parseInt(parts[0], 10);
    if (mm > 59) return null;
    const hours = parts[1].split(',');
    for (const hs of hours) {
      if (!/^\d+$/.test(hs)) return null;
      const hh = parseInt(hs, 10);
      if (hh > 23) return null;
      times.push({ hour: hh, minute: mm });
    }
    const segTail = [parts[2], parts[3], parts[4]];
    if (tail === null) tail = segTail;
    else if (tail.join(' ') !== segTail.join(' ')) return null;
  }
  if (times.length < 1 || times.length > 3) return null;
  return { times, tail };
}

function buildBroadcastCron(times, tail) {
  const t = tail || ['*', '*', '*'];
  const seen = {};
  const uniq = [];
  for (const it of times) {
    const key = it.hour * 60 + it.minute;
    if (seen[key]) continue;
    seen[key] = 1;
    uniq.push(it);
  }
  uniq.sort((a, b) => (a.hour * 60 + a.minute) - (b.hour * 60 + b.minute));
  return uniq
    .map((it) => it.minute + ' ' + it.hour + ' ' + t[0] + ' ' + t[1] + ' ' + t[2])
    .join(';');
}

function cronZh(cron) {
  const parts = String(cron || '').trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute, hour, dom, month, dow] = parts;
  if (month !== '*') return null;
  const times = () => {
    if (!/^\d+$/.test(minute)) return null;
    const hours = hour.split(',');
    if (!hours.every((h) => /^\d+$/.test(h))) return null;
    return hours
      .map((h) => String(parseInt(h, 10)).padStart(2, '0') + ':' +
        String(parseInt(minute, 10)).padStart(2, '0'))
      .join('、');
  };
  if (dom === '*' && dow === '*') {
    if (minute.startsWith('*/') && hour === '*' && /^\d+$/.test(minute.slice(2))) {
      return '每 ' + parseInt(minute.slice(2), 10) + ' 分钟';
    }
    if (hour === '*' && /^\d+$/.test(minute)) {
      return '每小时第 ' + parseInt(minute, 10) + ' 分';
    }
    const t = times();
    return t ? '每天 ' + t : null;
  }
  if (dom === '*' && dow !== '*') {
    const t = times();
    if (dow === '1-5' && t) return '工作日（周一至周五）' + t;
    const days = dow.split(',');
    if (t && days.every((d) => d in CRON_DOW)) {
      return '每周' + days.map((d) => CRON_DOW[d]).join('、周') + ' ' + t;
    }
    return null;
  }
  if (dom !== '*' && dow === '*' && /^\d+$/.test(dom)) {
    const t = times();
    return t ? '每月 ' + parseInt(dom, 10) + ' 日 ' + t : null;
  }
  return null;
}

function cronZhSegs(cron) {
  const segs = cronSegments(cron);
  const parts = [];
  for (const seg of segs) {
    const zh = cronZh(seg);
    if (!zh) return null;
    parts.push(zh);
  }
  return parts.length ? parts.join('；') : null;
}

const hourOptions = Array.from({ length: 24 }, (_, i) => i);
const minuteOptions = Array.from({ length: 60 }, (_, i) => i);

const schedOpen = ref(false);
const schedSid = ref('');
const schedCron = ref('');
const schedParsed = ref(null);
const schedTail = ref(null);
const schedSlots = ref([]);

const schedRuleZh = computed(
  () => cronZhSegs(schedCron.value) || schedCron.value || '常驻');

const schedPreview = computed(() => {
  if (!schedParsed.value) return '';
  const cron = buildBroadcastCron(schedSlots.value, schedTail.value);
  const zh = cronZhSegs(cron);
  return (zh ? '将改为：' + zh : '将改为') + '（' + cron + '）';
});

function openSchedule(s) {
  schedSid.value = s.service_id;
  schedCron.value = s.schedule || '';
  const parsed = parseBroadcastTimes(schedCron.value);
  schedParsed.value = parsed;
  if (parsed) {
    schedTail.value = parsed.tail;
    schedSlots.value = parsed.times.map((t) => ({ hour: t.hour, minute: t.minute }));
  } else {
    schedTail.value = null;
    schedSlots.value = [];
  }
  schedOpen.value = true;
}

function closeScheduleEdit() {
  schedOpen.value = false;
}

function addScheduleSlot() {
  if (schedSlots.value.length >= 3) return;
  schedSlots.value.push({ hour: 18, minute: 0 });
}

function removeScheduleSlot(idx) {
  if (schedSlots.value.length <= 1) return;
  schedSlots.value.splice(idx, 1);
}

async function submitScheduleEdit() {
  if (!schedParsed.value) return;
  const times = schedSlots.value.slice();
  if (!times.length) {
    window.alert('请至少保留一个时点');
    return;
  }
  const seen = {};
  const uniq = [];
  for (const it of times) {
    const key = it.hour * 60 + it.minute;
    if (seen[key]) continue;
    seen[key] = 1;
    uniq.push(it);
  }
  uniq.sort((a, b) => (a.hour * 60 + a.minute) - (b.hour * 60 + b.minute));
  const label = uniq
    .map((it) => String(it.hour).padStart(2, '0') + ':' + String(it.minute).padStart(2, '0'))
    .join('、');
  const sid = schedSid.value;
  closeScheduleEdit();
  try {
    await updatePipelineTimes({ service_id: sid, times: uniq });
    setFlash('ok', '已改 ' + sid + ' 播报时间为 ' + label + '（≤30s 生效）');
    await load(false);
  } catch (err) {
    setFlash('err', err.message);
  }
}

/* -------------------- 告警设置（巡检线阈值/冷却，落注册表 params）---- */
//: 阈值选项（须与后端 inspection_alert.SEVERITY_ORDER 一致）。
const ALERT_THRESHOLDS = [
  { value: 'info', label: 'info（全部发现）' },
  { value: 'warning', label: 'warning（默认：中及以上）' },
  { value: 'critical', label: 'critical（仅严重）' },
];

const alertOpen = ref(false);
const alertSid = ref('');
const alertRow = ref(null);
const alertThreshold = ref('warning');
const alertCooldownHours = ref('24');
const alertSaving = ref(false);

/** 行内展示当前告警口径（阈值 · 冷却）。 */
function alertSummary(s) {
  const p = s.params || {};
  const th = p.alert_threshold || 'warning';
  const cd = p.alert_cooldown_hours ?? 24;
  return '阈值 ' + th + ' · 冷却 ' + cd + 'h';
}

function openAlertSettings(s) {
  alertSid.value = s.service_id;
  alertRow.value = s;
  alertThreshold.value = (s.params && s.params.alert_threshold) || 'warning';
  alertCooldownHours.value = String(
    (s.params && s.params.alert_cooldown_hours) ?? 24,
  );
  alertOpen.value = true;
}

function closeAlertSettings() {
  alertOpen.value = false;
  alertRow.value = null;
}

async function submitAlertSettings() {
  const hours = Number(alertCooldownHours.value);
  if (!Number.isFinite(hours) || hours < 1 || hours > 24) {
    window.alert('冷却小时数需为 1–24 的整数（24 = 每天最多一条）');
    return;
  }
  // read-modify-write：保留本行已有 params 的其他键，只覆盖告警两键。
  const extra = { ...((alertRow.value && alertRow.value.params) || {}) };
  delete extra.alert_threshold;
  delete extra.alert_cooldown_hours;
  const params = {
    ...extra,
    alert_threshold: alertThreshold.value,
    alert_cooldown_hours: hours,
  };
  const sid = alertSid.value;
  alertSaving.value = true;
  try {
    await updatePipelineParams({ service_id: sid, params });
    setFlash('ok', '已更新 ' + sid + ' 告警口径（≤30s 生效）');
    closeAlertSettings();
    await load(false);
  } catch (err) {
    setFlash('err', err.message);
  } finally {
    alertSaving.value = false;
  }
}

/* -------------------- 新增定时任务（预设 + 时点合成 cron）------------ */
const form = reactive({
  service_id: '',
  kind: 'business',
  category: '',
  schedule_preset: 'daily',
  schedule_hour: '18',
  schedule_minute: '0',
  schedule_dow: '1',
  schedule_dom: '1',
  schedule: '',
  enabled: true,
  depends_on: '',
  description: '',
});

const showTime = computed(
  () => form.schedule_preset !== 'custom' && form.schedule_preset !== 'hourly');
const showDow = computed(() => form.schedule_preset === 'weekly');
const showDom = computed(() => form.schedule_preset === 'monthly');
const showCustom = computed(() => form.schedule_preset === 'custom');

function cronFromForm() {
  const p = form.schedule_preset;
  const hh = String(parseInt(form.schedule_hour || '0', 10));
  const mm = String(parseInt(form.schedule_minute || '0', 10));
  if (p === 'daily') return mm + ' ' + hh + ' * * *';
  if (p === 'weekday') return mm + ' ' + hh + ' * * 1-5';
  if (p === 'weekly') return mm + ' ' + hh + ' * * ' + form.schedule_dow;
  if (p === 'monthly') return mm + ' ' + hh + ' ' + (parseInt(form.schedule_dom, 10) || 1) + ' * *';
  if (p === 'hourly') return '0 * * * *';
  return String(form.schedule || '').trim();
}

const schedulePreview = computed(() => {
  const cron = cronFromForm();
  const zh = cronZh(cron);
  return zh ? '将按：' + zh + '（' + cron + '）' : (cron ? '将按 cron：' + cron : '');
});

function resetForm() {
  form.service_id = '';
  form.kind = 'business';
  form.category = '';
  form.schedule_preset = 'daily';
  form.schedule_hour = '18';
  form.schedule_minute = '0';
  form.schedule_dow = '1';
  form.schedule_dom = '1';
  form.schedule = '';
  form.enabled = true;
  form.depends_on = '';
  form.description = '';
}

async function submitAdd() {
  const schedule = cronFromForm();
  if (!schedule) {
    window.alert('请填写自定义 cron 表达式（或改用预设）');
    return;
  }
  try {
    await addPipeline({
      service_id: form.service_id.trim(),
      kind: form.kind,
      category: form.category,
      schedule,
      enabled: form.enabled,
      depends_on: form.depends_on.split(/[\s,]+/).filter(Boolean),
      description: form.description.trim(),
    });
    setFlash('ok', '定时任务已新增');
    resetForm();
    await load(false);
  } catch (err) {
    setFlash('err', err.message);
  }
}

/* ------------------------- 收件人覆盖抽屉 ---------------------------- */
const MODE_LABEL = { include: '追加', exclude: '移除' };
const TYPE_LABEL = { user_id: '账号', name: '姓名' };

const drawerOpen = ref(false);
const drawerService = ref('');
const drawerLoading = ref(false);
const drawerMsg = ref('');
const drawerBase = ref([]);
const drawerBaseSource = ref('');
const drawerExplain = ref([]);
const drawerRows = ref([]);
const addForm = reactive({ mode: 'include', target_type: 'user_id', target_value: '', note: '' });

async function loadDrawer() {
  drawerLoading.value = true;
  try {
    const data = await getRecipientOverride(drawerService.value);
    drawerBase.value = (data && data.base) || [];
    drawerBaseSource.value = (data && data.base_source) || '';
    drawerExplain.value = (data && data.explain) || [];
    drawerRows.value = (data && data.overrides) || [];
  } catch (err) {
    drawerMsg.value = '读取异常：' + err.message;
    drawerBase.value = [];
    drawerBaseSource.value = '';
    drawerExplain.value = [];
    drawerRows.value = [];
  } finally {
    drawerLoading.value = false;
  }
}

async function openDrawer(serviceId) {
  drawerService.value = serviceId;
  drawerMsg.value = '';
  drawerOpen.value = true;
  await loadDrawer();
}

function closeDrawer() {
  drawerOpen.value = false;
}

async function drawerAdd() {
  const value = addForm.target_value.trim();
  if (!value) return;
  drawerMsg.value = '';
  try {
    const data = await addRecipientOverride({
      service_id: drawerService.value,
      mode: addForm.mode,
      target_type: addForm.target_type,
      target_value: value,
      note: addForm.note.trim(),
    });
    const unresolved = (data && data.unresolved) || [];
    if (unresolved.length) {
      drawerMsg.value = '已保存，但 ' + unresolved.join('、') +
        ' 未在通讯录唯一命中，发送时会跳过（不会 @ 错人）';
    }
    addForm.target_value = '';
    addForm.note = '';
    await loadDrawer();
  } catch (err) {
    drawerMsg.value = '操作失败：' + err.message;
  }
}

async function drawerToggle(row) {
  drawerMsg.value = '';
  try {
    await toggleRecipientOverride({ id: row.id, enabled: !row.enabled });
    await loadDrawer();
  } catch (err) {
    drawerMsg.value = '操作失败：' + err.message;
  }
}

async function drawerDelete(row) {
  if (!window.confirm('确认删除该条覆盖？（审计留存被删内容）')) return;
  drawerMsg.value = '';
  try {
    await deleteRecipientOverride({ id: row.id });
    await loadDrawer();
  } catch (err) {
    drawerMsg.value = '操作失败：' + err.message;
  }
}

async function drawerReset() {
  if (!window.confirm('确认清空 ' + drawerService.value + ' 的全部覆盖，回退名册口径？')) return;
  drawerMsg.value = '';
  try {
    await resetRecipientOverride({ service_id: drawerService.value });
    await loadDrawer();
  } catch (err) {
    drawerMsg.value = '操作失败：' + err.message;
  }
}

/* ------------------------------ 生命周期 ----------------------------- */
onMounted(async () => {
  // 服务端 reminder_html 内联入口 onclick=openRecipientOverride('<id>')：
  // SPA 下桥接到本组件抽屉（挂载期生效，卸载即撤）。
  window.openRecipientOverride = (sid) => openDrawer(String(sid));
  await load();
  const deep = route.query.service;
  if (typeof deep === 'string' && deep) await openDrawer(deep);
  await applyRegionAnchor();
});

watch(() => route.query.region, () => applyRegionAnchor());

onBeforeUnmount(() => {
  if (window.openRecipientOverride) delete window.openRecipientOverride;
});
</script>

<template>
  <div class="page">
    <div class="head">
      <div class="head-main">
        <h1>定时任务（管道注册表）</h1>
        <p class="note">
          按管理群视角：同一区域的服务归为一组，一眼看清「这个群会收到哪些任务」；
          「渠道」「线下整体」为跨区域线，单独成组；无绑定区域的应用线/平台服务
          归入「平台 / 无群」。点分组标题可折叠。
        </p>
      </div>
      <div class="head-actions">
        <router-link class="btn" to="/pipelines/audit">任务审计</router-link>
        <router-link class="btn" to="/pipelines/history">执行流水</router-link>
      </div>
    </div>

    <div v-if="loading" class="state">加载中…</div>
    <div v-else-if="loadError" class="state error">
      {{ loadError }}
      <div><button class="btn" @click="load()">重试</button></div>
    </div>

    <template v-else>
      <details
        v-for="g in groups"
        :id="'group-' + (g.region ?? '__none__')"
        :key="g.region ?? '__none__'"
        class="group"
        :class="{ 'anchor-hit': anchorRegion && g.region === anchorRegion }"
        open
      >
        <summary>{{ g.title }}（{{ g.count }} 项）</summary>
        <table>
          <thead>
            <tr>
              <th>服务标识</th>
              <th>收受影响人</th>
              <th>类别</th>
              <th>播报时间</th>
              <th>依赖</th>
              <th>说明</th>
              <th>告警口径</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in g.services" :key="s.service_id">
              <td>
                <span>{{ s.service_name }}</span>
                <span v-if="s.service_name !== s.service_id" class="hint">（{{ s.service_id }}）</span>
              </td>
              <td class="reminder-cell">
                <span v-if="s.is_reminder && s.reminder_html" v-html="s.reminder_html"></span>
                <span v-else class="hint">—</span>
              </td>
              <td>{{ s.category_label }}</td>
              <td>
                <template v-if="s.schedule">
                  <template v-if="s.schedule_zh">
                    {{ s.schedule_zh }}<span class="hint">（{{ s.schedule }}）</span>
                  </template>
                  <template v-else>{{ s.schedule }}</template>
                </template>
                <span v-else class="hint">常驻</span>
              </td>
              <td>{{ s.depends_on && s.depends_on.length ? s.depends_on.join(', ') : '—' }}</td>
              <td>{{ s.service_label }}</td>
              <td>
                <span v-if="s.alertable">{{ alertSummary(s) }}</span>
                <span v-else class="hint">—</span>
              </td>
              <td>
                <span>{{ s.enabled ? '启用' : '停用' }}</span>
                <span v-if="s.latest" class="hint">（{{ s.latest.status_label }} {{ s.latest.requested_by }} {{ s.latest.created_at }}）</span>
                <span v-else class="hint">（无运行记录）</span>
              </td>
              <td class="row-actions">
                <button class="btn mini" @click="toggleTask(s)">{{ s.enabled ? '停用' : '启用' }}</button>
                <button class="btn mini" @click="openSchedule(s)">改播报时间</button>
                <button v-if="s.alertable" class="btn mini" @click="openAlertSettings(s)">告警设置</button>
                <button v-if="s.has_command" class="btn mini" @click="runOnce(s)">运行一次</button>
                <button v-if="!s.enabled" class="btn mini" @click="archiveTask(s)">归档</button>
              </td>
            </tr>
          </tbody>
        </table>
      </details>
      <p v-if="!groups.length" class="empty">暂无定时任务。</p>

      <p class="hint">
        「播报时间」= 该任务的定时规则（中文为主、原 cron 小字备查；用行内
        「改播报时间」可改，最多 3 个 HH:MM 时点）。配置存 Nacos 注册表
        （PIPELINES 组），开关与新增在下一个调度轮询（≤30 秒）生效；「收受影响人」
        = 催办覆盖面（日报机器人=名册在册人员，名册未登记则该区域在册填报人
        全员兜底，按部门归组展示；渠道到齐=名册店铺负责人），非催办行无。
      </p>

      <h2>新增定时任务</h2>
      <form class="inline" @submit.prevent="submitAdd">
        <input v-model="form.service_id" placeholder="服务标识（小写字母/数字/-/_）" required>
        <select v-model="form.kind">
          <option value="business">业务线</option>
          <option value="apps">应用线</option>
        </select>
        <select v-model="form.category">
          <option value="">功能（留空按标识推导）</option>
          <option v-for="c in supportedCategories" :key="c" :value="c">{{ c }}</option>
        </select>
        <select v-model="form.schedule_preset">
          <option value="daily">每天</option>
          <option value="weekday">工作日（周一至周五）</option>
          <option value="weekly">每周</option>
          <option value="monthly">每月</option>
          <option value="hourly">每小时整点</option>
          <option value="custom">自定义 cron</option>
        </select>
        <template v-if="showTime">
          <select v-model="form.schedule_hour">
            <option v-for="h in hourOptions" :key="h" :value="String(h)">{{ String(h).padStart(2, '0') }}</option>
          </select><span class="hint">时</span>
          <select v-model="form.schedule_minute">
            <option v-for="m in minuteOptions" :key="m" :value="String(m)">{{ String(m).padStart(2, '0') }}</option>
          </select><span class="hint">分</span>
        </template>
        <select v-if="showDow" v-model="form.schedule_dow">
          <option value="1">周一</option>
          <option value="2">周二</option>
          <option value="3">周三</option>
          <option value="4">周四</option>
          <option value="5">周五</option>
          <option value="6">周六</option>
          <option value="0">周日</option>
        </select>
        <input
          v-if="showDom"
          v-model="form.schedule_dom"
          type="number" min="1" max="31" style="width:64px"
        >
        <input v-if="showCustom" v-model="form.schedule" placeholder="cron（分 时 日 月 周）">
        <span class="hint">{{ schedulePreview }}</span>
        <label><input v-model="form.enabled" type="checkbox"> 启用</label>
        <input v-model="form.depends_on" placeholder="依赖的服务标识，逗号分隔（可空）">
        <input v-model="form.description" placeholder="描述">
        <button type="submit">新增</button>
      </form>
      <p class="hint">
        robot-&lt;区域&gt;（填报提醒）/ robot-check-&lt;区域&gt;（催办未填+DING）/
        pages-&lt;区域&gt; / leaderboard-&lt;区域&gt; 家族自动按后缀解析区域，注册即可调度；
        其他任意标识也可注册，但需先在调度器命令表加命令模板后才能触发。
        已存在的标识请用「操作」列开关，不可重复新增。
      </p>
    </template>

    <!-- 改播报时间弹窗 -->
    <div v-if="schedOpen" class="modal-mask" @click.self="closeScheduleEdit()">
      <div class="modal-box">
        <div>修改 <b>{{ schedSid }}</b> 的播报时间</div>
        <div>当前播报时间：<span>{{ schedRuleZh }}</span></div>
        <div class="hint">
          原表达式：<span>{{ schedCron || '—' }}</span>
          （只改时/分，工作日/每月几号等形态保留；最多 3 个时点）
        </div>
        <template v-if="schedParsed">
          <div>
            <div v-for="(slot, idx) in schedSlots" :key="idx" class="sched-slot">
              <select v-model.number="slot.hour">
                <option v-for="h in hourOptions" :key="h" :value="h">{{ String(h).padStart(2, '0') }}</option>
              </select><span class="hint">时</span>
              <select v-model.number="slot.minute">
                <option v-for="m in minuteOptions" :key="m" :value="m">{{ String(m).padStart(2, '0') }}</option>
              </select><span class="hint">分</span>
              <button
                type="button" class="btn mini"
                :disabled="schedSlots.length <= 1"
                @click="removeScheduleSlot(idx)"
              >✕</button>
            </div>
          </div>
          <div>
            <button
              type="button" class="btn mini"
              :disabled="schedSlots.length >= 3"
              @click="addScheduleSlot()"
            >＋时点</button>
          </div>
        </template>
        <div v-else class="hint">
          该任务非「每天固定时刻」形态（或已超 3 个时点），不支持按播报时间编辑。
        </div>
        <div class="hint">{{ schedPreview }}</div>
        <div class="modal-btns">
          <button :disabled="!schedParsed" @click="submitScheduleEdit()">确定</button>
          <button @click="closeScheduleEdit()">取消</button>
        </div>
      </div>
    </div>

    <!-- 告警设置弹窗（巡检线阈值/冷却） -->
    <div v-if="alertOpen" class="modal-mask" @click.self="closeAlertSettings()">
      <div class="modal-box">
        <div>修改 <b>{{ alertSid }}</b> 的告警口径</div>
        <div class="hint">
          低于阈值的发现不告警；冷却窗口内同一线只发一次（24h = 每天最多一条）。
          配置存 Nacos 注册表（PIPELINES 组），≤30 秒生效。
        </div>
        <div class="alert-row">
          <label>告警阈值</label>
          <select v-model="alertThreshold">
            <option v-for="t in ALERT_THRESHOLDS" :key="t.value" :value="t.value">{{ t.label }}</option>
          </select>
        </div>
        <div class="alert-row">
          <label>冷却（小时）</label>
          <input
            v-model="alertCooldownHours"
            type="number" min="1" max="24" style="width:80px"
          >
        </div>
        <div class="modal-btns">
          <button :disabled="alertSaving" @click="submitAlertSettings()">确定</button>
          <button @click="closeAlertSettings()">取消</button>
        </div>
      </div>
    </div>

    <!-- 收件人覆盖抽屉 -->
    <div v-if="drawerOpen" class="drawer-mask" @click.self="closeDrawer()">
      <div class="drawer">
        <h2 class="drawer-title">收件人覆盖 · {{ drawerService }}</h2>
        <div v-if="drawerMsg" class="flash err">{{ drawerMsg }}</div>
        <div v-if="drawerLoading" class="state">加载中…</div>
        <template v-else>
          <div class="base-box">
            <h3>本线基名单（不含下方覆盖）</h3>
            <p class="hint">来源：{{ drawerBaseSource || '—' }}</p>
            <p v-if="drawerBase.length">
              共 {{ drawerBase.length }} 人：{{ drawerBase.map((p) => p.name || p.user_id).join('、') }}
            </p>
            <p v-else class="hint">本线暂无基名单（触发时没有可 @ 的人，请按下述来源核查）。</p>
            <p class="hint">
              下方覆盖只影响本任务（{{ drawerService }}）；要增删整片区域的填报人请去
              <router-link to="/roster">填报名册</router-link>，
              要调整在职人员请去 <router-link to="/org">成员与授权</router-link>。
            </p>
          </div>

          <details v-if="drawerExplain.length" class="explain-box">
            <summary>口径说明：为什么有人不在名单（{{ drawerExplain.length }} 类）</summary>
            <ul>
              <li v-for="g in drawerExplain" :key="g.key || g.reason">
                <b>{{ g.reason }}</b>（{{ (g.items || []).length }} 人）：
                {{ (g.items || []).map((p) => p.name || p.user_id).join('、') }}
              </li>
            </ul>
            <p class="hint">
              有效 @ 名单 = （在职成员 ∩ 名册）∪ 追加 − 移除。整片区域的资格改
              <router-link to="/roster">填报名册</router-link>，
              在职状态查 <router-link to="/org">成员与授权</router-link>，
              本任务的例外用下方覆盖。
            </p>
          </details>

          <p class="hint">
            只改 @ 名单，不改消息正文；机器人在触发时套用。「追加」加入、「移除」剔除；
            停用或删除即回退名册口径。姓名型需在通讯录唯一命中才会 @（未命中会提示、
            不会 @ 错人）。
          </p>

          <table v-if="drawerRows.length">
            <thead>
              <tr>
                <th>方向</th><th>目标类型</th><th>值</th><th>解析账号</th><th>备注</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="o in drawerRows" :key="o.id">
                <td>{{ MODE_LABEL[o.mode] || o.mode }}</td>
                <td>{{ TYPE_LABEL[o.target_type] || o.target_type }}</td>
                <td>{{ o.target_value }}</td>
                <td>
                  {{ o.resolved_user_id || '—' }}
                  <span v-if="!o.enabled" class="hint">（已停用）</span>
                </td>
                <td>{{ o.note || '' }}</td>
                <td>
                  <button class="btn mini" @click="drawerToggle(o)">{{ o.enabled ? '停用' : '启用' }}</button>
                  <button class="btn mini" @click="drawerDelete(o)">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-else class="hint">暂无覆盖（当前按名册口径发送）。</p>

          <form class="inline" @submit.prevent="drawerAdd">
            <select v-model="addForm.mode">
              <option value="include">追加（加人）</option>
              <option value="exclude">移除（去人）</option>
            </select>
            <select v-model="addForm.target_type">
              <option value="user_id">账号</option>
              <option value="name">姓名</option>
            </select>
            <input v-model="addForm.target_value" placeholder="账号或姓名" required>
            <input v-model="addForm.note" placeholder="备注（可空）">
            <button type="submit">添加</button>
          </form>
          <div class="drawer-actions">
            <button class="btn danger" @click="drawerReset()">清空该线覆盖</button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.group {
  margin: 16px 0;
}
.group.anchor-hit {
  padding: 6px 10px;
  border-radius: 8px;
  background: #eef4ff;
  box-shadow: 0 0 0 2px #bfdbfe;
}
.group > summary {
  cursor: pointer;
  font-size: 15px;
  font-weight: 600;
  padding: 6px 0;
}
.reminder-cell :deep(a.recipient-entry) {
  margin-left: 6px;
  font-size: 12px;
}
.row-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.sched-slot {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 6px 0;
}
.alert-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0;
}
.alert-row label {
  min-width: 96px;
}
.base-box h3 {
  margin: 12px 0 4px;
  font-size: 14px;
}
.explain-box {
  margin: 12px 0;
}
.explain-box ul {
  margin: 6px 0;
  padding-left: 18px;
}
</style>
